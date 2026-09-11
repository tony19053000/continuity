"""C7-02: produce the patch, under rules the patch cannot talk its way out of.

The agent proposes edits. Every one of them is checked here, deterministically,
before a byte reaches disk:

* **Scope.** An edit to a file outside the correlated impact set (C6-02) is
  discarded unless the agent set `out_of_impact_justification` on it. This
  replaces any subjective notion of a "minimal" diff with something checkable:
  the diff's file list is compared against the impact set.
* **Secrets.** An edit whose content matches a credential pattern is discarded
  and becomes a DENY finding. It is refused rather than written and reported,
  because a secret on disk in a workspace is already a secret that a later
  command could echo.
* **Tests.** A test file cannot be deleted — `FileEdit` has no delete operation
  — and any edit that reduces the number of tests in a file is a finding. So is
  editing a test without declaring it. `02_ARCHITECTURE.md` §12: a failing test
  may not be weakened to reach PASS, and if the engineer believes a test is
  wrong that becomes a review finding rather than a silent edit.
* **Dependencies.** New dependencies are detected by parsing the manifests
  before and after, not by trusting the agent's own list — an omission there
  would otherwise slip a package past the supply-chain review
  (`03_SECURITY_ACCESS.md` §9). They are ASK.

Nothing in this module writes to the database or moves a run. It produces a
patch and a set of findings; `backend/orchestration/repair.py` decides what
happens next.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Final

from backend.agents.contracts import (
    AnalyzedChange,
    FileEdit,
    ImpactAnalystOutput,
    MigrationEngineerInput,
    MigrationEngineerOutput,
)
from backend.agents.specialists import MigrationEngineerAgent
from backend.migrations.workspace import MigrationWorkspace, WorkspaceWriteRejected
from backend.models.enums import (
    Confidence,
    EvidenceKind,
    FindingCategory,
    PolicyDecision,
    Severity,
)
from backend.models.schemas import Evidence
from backend.observability.logging import get_logger
from backend.security.policy import Action, ActionContext, PolicyEngine
from backend.shared.model_provider import ModelProvider
from backend.shared.redaction import contains_secret, detected_secret_kinds

logger = get_logger(__name__)

#: Path shapes that are test code. Deliberately broad: a false positive costs a
#: declaration, a false negative lets a test be quietly rewritten.
_TEST_PATH: Final = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|[^/]+_test\.(py|go|ts|js)$"
    r"|[^/]+\.(test|spec)\.(ts|tsx|js|jsx)$",
    re.IGNORECASE,
)

#: How a test declaration is spelled, per ecosystem. Counting these is how a
#: weakened test is detected without understanding the code.
_TEST_DECLARATION: Final = re.compile(
    r"(^|\s)(def\s+test_\w+|it\s*\(|test\s*\(|describe\s*\(|func\s+Test\w+)",
    re.MULTILINE,
)

_MANIFESTS: Final = frozenset(
    {"pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml"}
)

MAX_FILES_IN_CONTEXT: Final = 20


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def count_tests(content: str) -> int:
    return len(_TEST_DECLARATION.findall(content))


@dataclass(frozen=True, slots=True)
class PatchFinding:
    """A security finding produced by inspecting the patch itself.

    `policy_decision` comes from `backend/security/policy.py`. Nothing here asks
    a model what should be allowed.
    """

    category: FindingCategory
    severity: Severity
    summary: str
    policy_decision: PolicyDecision
    path: str | None = None

    @property
    def blocking(self) -> bool:
        return self.policy_decision is not PolicyDecision.ALLOW

    def evidence(self) -> Evidence:
        return Evidence(
            kind=EvidenceKind.SOURCE,
            confidence=Confidence.CONFIRMED,
            file_path=self.path,
            excerpt=self.summary[:1000],
        )

    def summary_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "policy_decision": self.policy_decision.value,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class RejectedEdit:
    path: str
    reason: str


@dataclass(slots=True)
class MigrationPatch:
    """What one attempt produced."""

    plan_summary: str
    applied: list[str] = field(default_factory=list)
    rejected: list[RejectedEdit] = field(default_factory=list)
    diff: str = ""
    findings: list[PatchFinding] = field(default_factory=list)
    modifies_tests: bool = False
    declared_test_modification: bool = False
    test_modification_justification: str = ""
    new_dependencies: list[str] = field(default_factory=list)
    diagnosis: str = ""

    @property
    def blocking_findings(self) -> list[PatchFinding]:
        return [finding for finding in self.findings if finding.blocking]

    @property
    def is_empty(self) -> bool:
        return not self.applied

    def summary(self) -> dict[str, object]:
        return {
            "plan_summary": self.plan_summary,
            "applied": sorted(self.applied),
            "rejected": [{"path": r.path, "reason": r.reason} for r in self.rejected],
            "modifies_tests": self.modifies_tests,
            "declared_test_modification": self.declared_test_modification,
            "test_modification_justification": self.test_modification_justification,
            "new_dependencies": sorted(self.new_dependencies),
            "findings": [finding.summary_dict() for finding in self.findings],
            "diagnosis": self.diagnosis,
        }


async def produce_patch(
    workspace: MigrationWorkspace,
    *,
    provider_id: str,
    from_version: str,
    to_version: str,
    change: AnalyzedChange,
    impact: ImpactAnalystOutput,
    impact_set: list[str],
    model_provider: ModelProvider,
    attempt_number: int = 1,
    previous_failure: str | None = None,
) -> MigrationPatch:
    """Ask for a patch, then apply only the parts that survive the rules."""
    contents = _read_context(workspace, impact_set)
    # The tests covering the impacted code, read-only. The engineer has to see
    # how the code is called to keep the call compatible — without them it
    # writes patches that are correct in isolation and break every caller.
    references = _read_context(
        workspace, [path for path in impact.affected_tests if path not in contents]
    )

    agent = MigrationEngineerAgent(model_provider)
    output: MigrationEngineerOutput = await agent.run(
        MigrationEngineerInput(
            provider_id=provider_id,
            from_version=from_version,
            to_version=to_version,
            change=change,
            impact=impact,
            impact_set=list(impact_set),
            file_contents=contents,
            reference_contents=references,
            previous_failure=previous_failure,
            attempt_number=attempt_number,
        )
    )

    patch = MigrationPatch(
        plan_summary=output.plan_summary,
        declared_test_modification=output.modifies_tests,
        test_modification_justification=output.test_modification_justification,
        diagnosis=output.diagnosis,
    )

    manifests_before = _read_manifests(workspace)
    allowed = {path for path in impact_set}

    for edit in output.edits:
        _apply_one(workspace, edit, allowed=allowed, patch=patch)

    patch.diff = await workspace.diff()
    _check_dependencies(workspace, manifests_before, output, patch)
    _check_test_declaration(patch)

    logger.info(
        "continuity.patch_produced",
        extra={
            "attempt": attempt_number,
            "applied": len(patch.applied),
            "rejected": len(patch.rejected),
            "findings": len(patch.findings),
            "blocking": len(patch.blocking_findings),
        },
    )
    return patch


# ---------------------------------------------------------------------------
# Per-edit rules
# ---------------------------------------------------------------------------


def _apply_one(
    workspace: MigrationWorkspace,
    edit: FileEdit,
    *,
    allowed: set[str],
    patch: MigrationPatch,
) -> None:
    engine = PolicyEngine()

    # --- secrets, first: nothing credential-shaped reaches disk ----------
    if contains_secret(edit.new_content):
        kinds = ", ".join(detected_secret_kinds(edit.new_content))
        decision = engine.classify(Action.EXPOSE_CREDENTIALS)
        patch.rejected.append(
            RejectedEdit(path=edit.path, reason=f"contains a credential ({kinds})")
        )
        patch.findings.append(
            PatchFinding(
                category=FindingCategory.SECRET_EXPOSURE,
                severity=Severity.CRITICAL,
                # The value is never quoted, here or in the finding.
                summary=(
                    f"The proposed patch for {edit.path} contains a credential "
                    f"({kinds}). The edit was discarded."
                ),
                policy_decision=decision.decision,
                path=edit.path,
            )
        )
        logger.warning(
            "continuity.patch_secret_rejected",
            extra={"path": edit.path, "kinds": kinds},
        )
        return

    # --- scope ------------------------------------------------------------
    if edit.path not in allowed and not edit.out_of_impact_justification:
        patch.rejected.append(
            RejectedEdit(
                path=edit.path,
                reason="outside the correlated impact set with no justification",
            )
        )
        patch.findings.append(
            PatchFinding(
                category=FindingCategory.TOOL_MISUSE,
                severity=Severity.LOW,
                summary=(
                    f"{edit.path} is not in the correlated impact set and the "
                    "edit carried no out-of-impact justification. Discarded "
                    "before it reached disk."
                ),
                # ALLOW, so this does not halt the run — and that is the whole
                # point of it being ALLOW. The edit never landed, so there is
                # nothing for a human to approve or refuse. Recorded because a
                # model repeatedly reaching outside its scope is worth seeing,
                # and fed back to the next attempt so it stops doing it. Making
                # this ASK escalated the run on the first out-of-scope guess and
                # left the repair budget unspent.
                policy_decision=PolicyDecision.ALLOW,
                path=edit.path,
            )
        )
        return

    # --- tests ------------------------------------------------------------
    if is_test_path(edit.path):
        before = workspace.read_file(edit.path) if workspace.exists(edit.path) else ""
        removed = count_tests(before) - count_tests(edit.new_content)
        patch.modifies_tests = True

        if removed > 0:
            decision = engine.classify(Action.WEAKEN_TEST)
            patch.rejected.append(
                RejectedEdit(
                    path=edit.path,
                    reason=f"removes {removed} test(s)",
                )
            )
            patch.findings.append(
                PatchFinding(
                    category=FindingCategory.TEST_WEAKENED,
                    severity=Severity.HIGH,
                    summary=(
                        f"The patch removes {removed} test(s) from {edit.path}. "
                        "A failing test may not be deleted or weakened to reach "
                        "PASS; the edit was discarded."
                    ),
                    policy_decision=decision.decision,
                    path=edit.path,
                )
            )
            return

    # --- write ------------------------------------------------------------
    context = ActionContext(target_path=edit.path, inside_workspace=True)
    decision = engine.classify(Action.MODIFY_WORKSPACE_FILE, context)
    if not decision.allowed:
        patch.rejected.append(RejectedEdit(path=edit.path, reason=decision.reason))
        return

    try:
        workspace.write_file(edit.path, edit.new_content)
    except WorkspaceWriteRejected as rejection:
        patch.rejected.append(RejectedEdit(path=edit.path, reason=str(rejection)))
        patch.findings.append(
            PatchFinding(
                category=FindingCategory.TOOL_MISUSE,
                severity=Severity.HIGH,
                summary=f"The patch tried to write outside the workspace: {rejection}",
                policy_decision=PolicyDecision.DENY,
                path=edit.path,
            )
        )
        return

    patch.applied.append(edit.path)


def _check_test_declaration(patch: MigrationPatch) -> None:
    """A test edit is a finding whether or not it was declared.

    Declared or not, a human decides — the difference is what the finding says.
    An undeclared test edit is the worse of the two, because it is the shape of
    a test being quietly adjusted until it passes.
    """
    if not patch.modifies_tests:
        return

    engine = PolicyEngine()
    decision = engine.classify(Action.WEAKEN_TEST)

    if patch.declared_test_modification:
        summary = (
            "The patch modifies test files, declared by the engineer: "
            f"{patch.test_modification_justification or 'no justification given'}"
        )
        severity = Severity.MEDIUM
    else:
        summary = (
            "The patch modifies test files without declaring it. A test change "
            "is a human decision, not the engineer's."
        )
        severity = Severity.HIGH

    patch.findings.append(
        PatchFinding(
            category=FindingCategory.TEST_WEAKENED,
            severity=severity,
            summary=summary,
            policy_decision=decision.decision,
        )
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _read_context(workspace: MigrationWorkspace, impact_set: list[str]) -> dict[str, str]:
    """The files the engineer is shown. Bounded, and never the repository.

    `CLAUDE.md` §3.7 — context goes through the deterministic index and bounded
    retrieval, never a repository dump.
    """
    contents: dict[str, str] = {}
    for path in impact_set[:MAX_FILES_IN_CONTEXT]:
        try:
            if workspace.exists(path):
                contents[path] = workspace.read_file(path)
        except (WorkspaceWriteRejected, OSError):
            continue
    return contents


def _read_manifests(workspace: MigrationWorkspace) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in sorted(_MANIFESTS):
        try:
            if workspace.exists(name):
                found[name] = workspace.read_file(name)
        except (WorkspaceWriteRejected, OSError):
            continue
    return found


def declared_dependencies(manifest_name: str, content: str) -> set[str]:
    """Dependency names in a manifest. Parsed, not pattern-matched.

    Returns an empty set when the manifest will not parse: a half-read manifest
    would produce a phantom "new dependency" list built from parse damage.
    """
    try:
        if manifest_name == "pyproject.toml":
            data = tomllib.loads(content)
            names: set[str] = set()
            project = data.get("project", {})
            for spec in project.get("dependencies", []) or []:
                names.add(_package_name(str(spec)))
            for group in (project.get("optional-dependencies", {}) or {}).values():
                for spec in group or []:
                    names.add(_package_name(str(spec)))
            for group in (data.get("dependency-groups", {}) or {}).values():
                for spec in group or []:
                    names.add(_package_name(str(spec)))
            return {name for name in names if name}

        if manifest_name == "requirements.txt":
            return {
                _package_name(line)
                for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            } - {""}

        if manifest_name == "package.json":
            data = json.loads(content)
            names = set()
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                names.update((data.get(key) or {}).keys())
            return names
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        return set()

    return set()


def _package_name(spec: str) -> str:
    return re.split(r"[\s<>=!~\[;]", spec.strip(), maxsplit=1)[0].strip().lower()


def _check_dependencies(
    workspace: MigrationWorkspace,
    before: dict[str, str],
    output: MigrationEngineerOutput,
    patch: MigrationPatch,
) -> None:
    """Detect added dependencies by parsing manifests, not by asking.

    The agent's own `new_dependencies` is recorded too, and a package it failed
    to mention still produces the finding — an omission there would otherwise
    walk a package straight past the supply-chain review.
    """
    added: set[str] = set()

    for name in sorted(_MANIFESTS):
        if not workspace.exists(name):
            continue
        after_content = workspace.read_file(name)
        if after_content == before.get(name):
            continue
        added |= declared_dependencies(name, after_content) - declared_dependencies(
            name, before.get(name, "")
        )

    # Anything the agent declared counts even if no manifest changed: it may
    # intend to add it, and an intent to add is the thing being reviewed.
    added |= {_package_name(spec) for spec in output.new_dependencies} - {""}

    if not added:
        return

    patch.new_dependencies = sorted(added)
    decision = PolicyEngine().classify(Action.INSTALL_DEPENDENCY)
    patch.findings.append(
        PatchFinding(
            category=FindingCategory.NEW_DEPENDENCY,
            severity=decision.risk or Severity.MEDIUM,
            summary=(
                "The patch introduces new dependencies: "
                f"{', '.join(patch.new_dependencies)}. The supply chain is part "
                "of the review surface (03_SECURITY_ACCESS.md §9)."
            ),
            policy_decision=decision.decision,
        )
    )
