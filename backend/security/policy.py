"""Deterministic action authorization.

**LLM output never equals authorization.** An agent proposing an action is
making a request; this module decides. There is no model anywhere in this file,
and there is no code path that lets a proposed action reach a capability without
passing through `PolicyEngine.classify` — the tool dispatcher is the only caller
of the underlying tools, and it always classifies first.

The matrix below is the executable form of `03_SECURITY_ACCESS.md` §4. A test
in `tests/security/test_policy_matrix.py` asserts every documented row has a
classification here, so the document and the code cannot drift.

Default is DENY. An action nobody has classified is refused, not allowed —
adding a tool without deciding its risk should break loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from backend.models.enums import PolicyDecision, Severity


class Action(StrEnum):
    """Every action an agent may attempt.

    Named for what is being done rather than which tool does it, so two tools
    performing the same privileged operation cannot be classified differently.
    """

    # --- Repository (read) ---
    INSPECT_REPOSITORY = "inspect_repository"
    READ_REPOSITORY_FILE = "read_repository_file"

    # --- Provider (read) ---
    FETCH_PROVIDER_DOCS = "fetch_provider_docs"
    COMPARE_PROVIDER_CONTRACTS = "compare_provider_contracts"

    # --- Validation ---
    RUN_TESTS = "run_tests"

    # --- Migration workspace ---
    MODIFY_WORKSPACE_FILE = "modify_workspace_file"
    CREATE_CONTINUITY_BRANCH = "create_continuity_branch"
    CREATE_PULL_REQUEST = "create_pull_request"

    # --- Elevated: require a human ---
    INSTALL_DEPENDENCY = "install_dependency"
    EXPAND_OAUTH_SCOPE = "expand_oauth_scope"
    CHANGE_AUTHENTICATION = "change_authentication"
    MIGRATE_CREDENTIAL = "migrate_credential"
    PRODUCTION_OPERATION = "production_operation"
    DESTRUCTIVE_MIGRATION = "destructive_migration"
    REPOSITORY_ACTION_BEYOND_PR = "repository_action_beyond_pr"
    WEAKEN_TEST = "weaken_test"

    # --- Forbidden ---
    EXPOSE_CREDENTIALS = "expose_credentials"
    DELETE_REPOSITORY_OR_BRANCH = "delete_repository_or_branch"
    FORCE_PUSH = "force_push"
    REWRITE_HISTORY = "rewrite_history"
    WRITE_PROTECTED_BRANCH = "write_protected_branch"
    MERGE_PULL_REQUEST = "merge_pull_request"
    BYPASS_APPROVAL = "bypass_approval"
    DISABLE_SECURITY_CHECK = "disable_security_check"
    EXECUTE_UNLISTED_COMMAND = "execute_unlisted_command"
    READ_OUTSIDE_REPOSITORY = "read_outside_repository"


# The matrix. Every `Action` member must appear exactly once.
POLICY: Final[dict[Action, PolicyDecision]] = {
    Action.INSPECT_REPOSITORY: PolicyDecision.ALLOW,
    Action.READ_REPOSITORY_FILE: PolicyDecision.ALLOW,
    Action.FETCH_PROVIDER_DOCS: PolicyDecision.ALLOW,
    Action.COMPARE_PROVIDER_CONTRACTS: PolicyDecision.ALLOW,
    Action.RUN_TESTS: PolicyDecision.ALLOW,
    Action.MODIFY_WORKSPACE_FILE: PolicyDecision.ALLOW,
    Action.CREATE_CONTINUITY_BRANCH: PolicyDecision.ALLOW,
    Action.CREATE_PULL_REQUEST: PolicyDecision.ALLOW,
    Action.INSTALL_DEPENDENCY: PolicyDecision.ASK,
    Action.EXPAND_OAUTH_SCOPE: PolicyDecision.ASK,
    Action.CHANGE_AUTHENTICATION: PolicyDecision.ASK,
    Action.MIGRATE_CREDENTIAL: PolicyDecision.ASK,
    Action.PRODUCTION_OPERATION: PolicyDecision.ASK,
    Action.DESTRUCTIVE_MIGRATION: PolicyDecision.ASK,
    Action.REPOSITORY_ACTION_BEYOND_PR: PolicyDecision.ASK,
    Action.WEAKEN_TEST: PolicyDecision.ASK,
    Action.EXPOSE_CREDENTIALS: PolicyDecision.DENY,
    Action.DELETE_REPOSITORY_OR_BRANCH: PolicyDecision.DENY,
    Action.FORCE_PUSH: PolicyDecision.DENY,
    Action.REWRITE_HISTORY: PolicyDecision.DENY,
    Action.WRITE_PROTECTED_BRANCH: PolicyDecision.DENY,
    Action.MERGE_PULL_REQUEST: PolicyDecision.DENY,
    Action.BYPASS_APPROVAL: PolicyDecision.DENY,
    Action.DISABLE_SECURITY_CHECK: PolicyDecision.DENY,
    Action.EXECUTE_UNLISTED_COMMAND: PolicyDecision.DENY,
    Action.READ_OUTSIDE_REPOSITORY: PolicyDecision.DENY,
}

# Risk attached to each ASK, shown on the approval card so a human can judge
# quickly rather than reading the raw action.
ASK_RISK: Final[dict[Action, Severity]] = {
    Action.INSTALL_DEPENDENCY: Severity.MEDIUM,
    Action.EXPAND_OAUTH_SCOPE: Severity.HIGH,
    Action.CHANGE_AUTHENTICATION: Severity.HIGH,
    Action.MIGRATE_CREDENTIAL: Severity.HIGH,
    Action.PRODUCTION_OPERATION: Severity.HIGH,
    Action.DESTRUCTIVE_MIGRATION: Severity.CRITICAL,
    Action.REPOSITORY_ACTION_BEYOND_PR: Severity.MEDIUM,
    Action.WEAKEN_TEST: Severity.HIGH,
}


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Facts about a specific attempt, used to escalate a normally-safe action.

    An action's baseline classification is not always the whole story: writing a
    file is ALLOW inside a migration workspace and DENY outside it. Escalation
    is computed here, deterministically, never proposed by the agent.
    """

    target_path: str | None = None
    target_branch: str | None = None
    is_default_branch: bool = False
    inside_workspace: bool = True
    inside_repository: bool = True
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    action: Action
    reason: str
    risk: Severity | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW


class PolicyEngine:
    """Classifies an attempted action. Contains no model and no I/O."""

    def classify(self, action: Action, context: ActionContext | None = None) -> PolicyResult:
        ctx = context or ActionContext()

        # Context-based escalation runs first: a branch write that targets the
        # default branch is a different action from one that does not, whatever
        # the agent believed it was doing.
        escalated = self._escalate(action, ctx)
        if escalated is not None:
            return escalated

        decision = POLICY.get(action)
        if decision is None:
            # Unknown action: refuse. Adding a tool without classifying it
            # should fail loudly rather than inherit permission.
            return PolicyResult(
                decision=PolicyDecision.DENY,
                action=action,
                reason=f"{action.value} has no policy classification; refusing by default.",
            )

        if decision is PolicyDecision.ASK:
            return PolicyResult(
                decision=decision,
                action=action,
                reason=f"{action.value} requires human approval.",
                risk=ASK_RISK.get(action, Severity.HIGH),
            )

        return PolicyResult(
            decision=decision,
            action=action,
            reason=f"{action.value} is classified {decision.value}.",
        )

    def _escalate(self, action: Action, ctx: ActionContext) -> PolicyResult | None:
        """Turn a context-unsafe attempt into the action it really is."""
        if action in _BRANCH_WRITING and ctx.is_default_branch:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                action=Action.WRITE_PROTECTED_BRANCH,
                reason=(
                    f"Refused: {action.value} targeted the default branch "
                    f"{ctx.target_branch!r}. Continuity writes only to its own branches."
                ),
            )

        if action is Action.MODIFY_WORKSPACE_FILE and not ctx.inside_workspace:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                action=Action.WRITE_PROTECTED_BRANCH,
                reason=(
                    f"Refused: write to {ctx.target_path!r} is outside the migration workspace."
                ),
            )

        if action is Action.READ_REPOSITORY_FILE and not ctx.inside_repository:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                action=Action.READ_OUTSIDE_REPOSITORY,
                reason=f"Refused: {ctx.target_path!r} is outside the authorized repository.",
            )

        return None


_BRANCH_WRITING: Final = frozenset(
    {Action.CREATE_CONTINUITY_BRANCH, Action.CREATE_PULL_REQUEST, Action.MODIFY_WORKSPACE_FILE}
)

#: Process-wide engine. Stateless, so sharing one instance is safe.
policy_engine: Final = PolicyEngine()
