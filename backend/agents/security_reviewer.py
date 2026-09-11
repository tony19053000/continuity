"""C8-01: structured review of the generated diff.

Two halves, and the split is the point.

**Code decides what it can see.** `backend/security/categories.py` finds the
categories that are not a matter of judgment — a credential is a regex match, a
removed signature check is a token that was there and is not any more, a file
outside the impact set is arithmetic. These run whether or not a model is
reachable, so the security floor does not depend on one being available.

**The agent reviews what needs reading.** A diff can be wrong in ways no pattern
catches, and that is what the model is for. Its findings are merged with the
deterministic ones rather than replacing them: a category code already found is
not overwritten by the model's account of it.

**Neither of them decides.** `backend/security/policy.py` maps every category to
an action and classifies it, and that is the binding answer. The agent's
`recommendation` is stored beside `policy_decision` precisely so a disagreement
is visible — `03_SECURITY_ACCESS.md` §10 asks for it, and a persistent gap
between the two is worth investigating rather than smoothing over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from backend.agents.contracts import SecurityReviewerInput, SecurityReviewerOutput
from backend.agents.specialists import SecurityReviewerAgent
from backend.models.enums import (
    Confidence,
    EvidenceKind,
    FindingCategory,
    PolicyDecision,
    Severity,
)
from backend.models.schemas import Evidence
from backend.observability.logging import get_logger
from backend.security.categories import DetectedFinding, detect
from backend.security.policy import Action, PolicyEngine
from backend.shared.model_provider import ModelProvider
from backend.shared.redaction import redact

logger = get_logger(__name__)

#: Which action each finding category *is*, for policy purposes. This mapping is
#: the whole reason a finding can be classified without asking anyone: the
#: category names what happened, and policy already knows what that action costs.
CATEGORY_ACTION: Final[dict[FindingCategory, Action]] = {
    FindingCategory.SECRET_EXPOSURE: Action.EXPOSE_CREDENTIALS,
    FindingCategory.PRIVILEGE_EXPANSION: Action.EXPAND_OAUTH_SCOPE,
    FindingCategory.OAUTH_SCOPE_CHANGE: Action.EXPAND_OAUTH_SCOPE,
    FindingCategory.AUTHENTICATION_CHANGE: Action.CHANGE_AUTHENTICATION,
    FindingCategory.AUTHORIZATION_WEAKENED: Action.DISABLE_SECURITY_CHECK,
    FindingCategory.WEBHOOK_VERIFICATION: Action.DISABLE_SECURITY_CHECK,
    FindingCategory.UNSAFE_PARAMETER: Action.DESTRUCTIVE_MIGRATION,
    FindingCategory.DANGEROUS_RETRY: Action.DESTRUCTIVE_MIGRATION,
    FindingCategory.DUPLICATE_TRANSACTION_RISK: Action.DESTRUCTIVE_MIGRATION,
    FindingCategory.NEW_DEPENDENCY: Action.INSTALL_DEPENDENCY,
    FindingCategory.TOOL_MISUSE: Action.REPOSITORY_ACTION_BEYOND_PR,
    FindingCategory.PROMPT_INJECTION_SUSPECTED: Action.REPOSITORY_ACTION_BEYOND_PR,
    FindingCategory.TEST_WEAKENED: Action.WEAKEN_TEST,
}

#: How much diff the model sees. A patch larger than this is reviewed on its
#: deterministic findings alone, which is stated in the report rather than
#: quietly truncated.
MAX_DIFF_CHARS: Final = 60_000


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One finding, with both verdicts on it."""

    category: FindingCategory
    severity: Severity
    summary: str
    evidence: Evidence
    #: The agent's advice. Equal to `policy_decision` when code found it alone.
    recommendation: PolicyDecision
    policy_decision: PolicyDecision
    source: str  # "deterministic" or "security_reviewer"

    @property
    def blocking(self) -> bool:
        return self.policy_decision is not PolicyDecision.ALLOW

    @property
    def disagreed(self) -> bool:
        """Whether the agent and the policy engine reached different answers."""
        return self.recommendation is not self.policy_decision

    def summary_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "recommendation": self.recommendation.value,
            "policy_decision": self.policy_decision.value,
            "source": self.source,
            "disagreed": self.disagreed,
        }


@dataclass(slots=True)
class SecurityReview:
    """What the review concluded, and how it got there."""

    findings: list[ReviewFinding] = field(default_factory=list)
    #: The binding verdict: the strictest policy decision across all findings.
    decision: PolicyDecision = PolicyDecision.ALLOW
    agent_recommendation: PolicyDecision = PolicyDecision.ALLOW
    summary: str = ""
    model_consulted: bool = False
    diff_truncated: bool = False

    @property
    def passed(self) -> bool:
        return self.decision is PolicyDecision.ALLOW

    @property
    def blocking(self) -> list[ReviewFinding]:
        return [finding for finding in self.findings if finding.blocking]

    @property
    def disagreements(self) -> list[ReviewFinding]:
        return [finding for finding in self.findings if finding.disagreed]

    def report(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "agent_recommendation": self.agent_recommendation.value,
            "summary": self.summary,
            "model_consulted": self.model_consulted,
            "diff_truncated": self.diff_truncated,
            "findings": [finding.summary_dict() for finding in self.findings],
            "disagreements": len(self.disagreements),
        }


def classify_category(category: FindingCategory) -> PolicyDecision:
    """The binding decision for a finding category.

    A category with no mapping is DENY, not ALLOW: adding a `FindingCategory`
    without deciding what it costs should fail closed, and a test asserts the
    mapping is complete so this is a backstop rather than a routine path.
    """
    action = CATEGORY_ACTION.get(category)
    if action is None:
        return PolicyDecision.DENY
    return PolicyEngine().classify(action).decision


def _strictest(decisions: list[PolicyDecision]) -> PolicyDecision:
    if PolicyDecision.DENY in decisions:
        return PolicyDecision.DENY
    if PolicyDecision.ASK in decisions:
        return PolicyDecision.ASK
    return PolicyDecision.ALLOW


def _from_detected(finding: DetectedFinding) -> ReviewFinding:
    decision = classify_category(finding.category)
    return ReviewFinding(
        category=finding.category,
        severity=finding.severity,
        summary=redact(finding.summary),
        evidence=finding.evidence(),
        # Code found it, so there is no separate agent opinion to record. They
        # agree by construction, which is why `disagreed` is False here.
        recommendation=decision,
        policy_decision=decision,
        source="deterministic",
    )


async def review_patch(
    *,
    provider_id: str,
    diff: str,
    changed_dependencies: list[str],
    modifies_tests: bool,
    impact_set: list[str] | None = None,
    model_provider: ModelProvider | None = None,
) -> SecurityReview:
    """Review a patch. Code first, then the model, then policy decides."""
    review = SecurityReview()

    for detected in detect(diff, impact_set=impact_set):
        review.findings.append(_from_detected(detected))

    already_found = {finding.category for finding in review.findings}

    if model_provider is not None:
        truncated = diff[:MAX_DIFF_CHARS]
        review.diff_truncated = len(diff) > MAX_DIFF_CHARS
        await _consult(
            review,
            model_provider=model_provider,
            provider_id=provider_id,
            diff=truncated,
            changed_dependencies=changed_dependencies,
            modifies_tests=modifies_tests,
            already_found=already_found,
        )

    review.decision = _strictest(
        [finding.policy_decision for finding in review.findings]
    )
    if not review.model_consulted:
        review.agent_recommendation = review.decision

    if not review.summary:
        review.summary = (
            f"{len(review.findings)} finding(s); "
            f"{len(review.blocking)} require a decision."
            if review.findings
            else "No security findings in this patch."
        )

    logger.info(
        "continuity.security_review",
        extra={
            "findings": len(review.findings),
            "blocking": len(review.blocking),
            "decision": review.decision.value,
            "disagreements": len(review.disagreements),
            "model_consulted": review.model_consulted,
        },
    )
    return review


async def _consult(
    review: SecurityReview,
    *,
    model_provider: ModelProvider,
    provider_id: str,
    diff: str,
    changed_dependencies: list[str],
    modifies_tests: bool,
    already_found: set[FindingCategory],
) -> None:
    """Add what the model saw, without letting it overwrite what code saw."""
    agent = SecurityReviewerAgent(model_provider)
    try:
        output: SecurityReviewerOutput = await agent.run(
            SecurityReviewerInput(
                provider_id=provider_id,
                patch_diff=diff,
                changed_dependencies=list(changed_dependencies),
                modifies_tests=modifies_tests,
            )
        )
    except Exception as exc:
        # The deterministic findings stand. Losing the model's reading degrades
        # the review; treating its absence as "nothing found" would be a
        # security claim nobody made.
        logger.warning(
            "continuity.security_reviewer_failed", extra={"error": type(exc).__name__}
        )
        review.summary = (
            "The Security Reviewer could not be consulted; this review covers "
            "only the deterministic checks."
        )
        return

    review.model_consulted = True
    review.agent_recommendation = output.overall_recommendation
    review.summary = redact(output.summary)

    for proposed in output.findings:
        if proposed.category in already_found:
            # Code already found this category and quoted the line. The model's
            # version adds nothing and would double-count in the report.
            continue

        decision = classify_category(proposed.category)
        review.findings.append(
            ReviewFinding(
                category=proposed.category,
                severity=proposed.severity,
                summary=redact(proposed.summary),
                evidence=Evidence(
                    kind=EvidenceKind.SOURCE,
                    # INFERRED: the model read the diff and formed a view. Code
                    # that matched a pattern produces CONFIRMED.
                    confidence=Confidence.INFERRED,
                    file_path=proposed.evidence.file_path,
                    line_start=proposed.evidence.line_start,
                    line_end=proposed.evidence.line_end,
                    excerpt=redact(proposed.evidence.excerpt or proposed.summary)[:1000],
                ),
                recommendation=proposed.recommendation,
                policy_decision=decision,
                source="security_reviewer",
            )
        )

    for finding in review.disagreements:
        logger.warning(
            "continuity.security_review_disagreement",
            extra={
                "category": finding.category.value,
                "agent": finding.recommendation.value,
                "policy": finding.policy_decision.value,
            },
        )
