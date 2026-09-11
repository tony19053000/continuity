"""C9-02: what a merged migration did to the running application.

`backend/workers/post_merge.py` verifies that the merge is consistent with the
run — the right branch, the right pull request, the right version. That is
checkable from what Continuity already knows, and it is all that stage claims.
This module is the other half: the project's own synthetic checks, run against
its own environment, before the merge and after it.

**The comparison is the point.** A check that was already failing before the
merge is not a regression, and blaming the migration for it would teach a team
to ignore these reports. Code decides which is which — it is arithmetic over two
observations — and the agent explains what it means.

**It recommends; it never acts.** `rollback_recommended` is advice a person
reads. Nothing in Continuity reverts a commit, resets a branch, force-pushes, or
redeploys, and `tests/security/test_release_guardian_surface.py` asserts over
this module's source that no such path exists. An autonomous rollback triggered
by a failing health check is a bigger outage than the one it is responding to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agents.contracts import (
    CheckReport,
    ReleaseGuardianInput,
    ReleaseGuardianOutput,
)
from backend.agents.specialists import ReleaseGuardianAgent
from backend.observability.logging import get_logger
from backend.shared.model_provider import ModelProvider
from backend.shared.redaction import redact
from backend.verification.environment import Observation

logger = get_logger(__name__)

#: How many response bodies the model is shown. Bounded because they are
#: written by the deployed application, and because a verification response
#: that needs more than this to interpret is not a verification response.
MAX_EXCERPTS = 5


@dataclass(frozen=True, slots=True)
class Regression:
    """A check that worked before the merge and does not now."""

    name: str
    reason: str
    status: int | None

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "reason": self.reason, "status": self.status}


@dataclass(slots=True)
class ReleaseAssessment:
    """What the checks showed, and what the Guardian makes of it."""

    #: The binding verdict, computed from the two observations. Not the agent's.
    passed: bool = False
    regressions: list[Regression] = field(default_factory=list)
    #: Failing now and failing before. Reported, not blamed on this migration.
    already_failing: list[str] = field(default_factory=list)
    #: Failing now with no pre-merge observation to compare against.
    unattributable: list[str] = field(default_factory=list)
    summary: str = ""
    #: Advisory. Nothing reads this to decide anything.
    rollback_recommended: bool = False
    likely_related_to_migration: bool = False
    rationale: str = ""
    model_consulted: bool = False
    compared_with_baseline: bool = False

    def report(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "compared_with_baseline": self.compared_with_baseline,
            "regressions": [item.summary() for item in self.regressions],
            "already_failing": sorted(self.already_failing),
            "unattributable": sorted(self.unattributable),
            "summary": self.summary,
            "model_consulted": self.model_consulted,
            # Stored so a person can read it. No code branches on it.
            "rollback_recommended": self.rollback_recommended,
            "likely_related_to_migration": self.likely_related_to_migration,
            "rationale": self.rationale,
            "action_taken": "none: Continuity never performs a rollback",
        }


async def assess_release(
    *,
    provider_id: str,
    from_version: str,
    to_version: str,
    before: Observation | None,
    after: Observation,
    changed_files: list[str] | None = None,
    model_provider: ModelProvider | None = None,
) -> ReleaseAssessment:
    """Compare the two observations, then ask what they mean.

    `before` is `None` when no pre-merge observation was recorded — a run
    delivered before this was configured, or an environment that could not be
    reached at the time. The comparison then cannot attribute a failure to the
    migration, and says so rather than assuming either way.
    """
    assessment = ReleaseAssessment(compared_with_baseline=before is not None)

    previous = before.by_name() if before is not None and before.reached else {}

    for outcome in after.outcomes:
        if outcome.ok:
            continue
        earlier = previous.get(outcome.name)
        if earlier is None:
            assessment.unattributable.append(outcome.name)
        elif earlier.ok:
            assessment.regressions.append(
                Regression(
                    name=outcome.name,
                    reason=redact(outcome.reason),
                    status=outcome.status,
                )
            )
        else:
            assessment.already_failing.append(outcome.name)

    # Fail closed on anything failing now, whether or not it can be attributed.
    # "We cannot tell whether the migration broke it" is not a reason to call a
    # broken check fine; it is a reason to put it in front of a person.
    assessment.passed = (
        after.reached and not assessment.regressions and not assessment.unattributable
    )

    if not after.reached:
        assessment.summary = (
            "The environment could not be reached, so nothing was verified."
        )
    elif assessment.passed:
        assessment.summary = (
            f"{len(after.outcomes)} declared check(s) passed after the merge"
            + (
                f"; {len(assessment.already_failing)} were already failing before it."
                if assessment.already_failing
                else "."
            )
        )
    else:
        assessment.summary = (
            f"{len(assessment.regressions)} regression(s) and "
            f"{len(assessment.unattributable)} unattributable failure(s) after "
            "the merge."
        )

    if model_provider is not None and after.reached:
        await _consult(
            assessment,
            model_provider=model_provider,
            provider_id=provider_id,
            from_version=from_version,
            to_version=to_version,
            before=previous,
            after=after,
            changed_files=changed_files or [],
        )

    logger.info(
        "continuity.release_guardian",
        extra={
            "passed": assessment.passed,
            "regressions": len(assessment.regressions),
            "unattributable": len(assessment.unattributable),
            "already_failing": len(assessment.already_failing),
            "compared_with_baseline": assessment.compared_with_baseline,
            "model_consulted": assessment.model_consulted,
        },
    )
    return assessment


async def _consult(
    assessment: ReleaseAssessment,
    *,
    model_provider: ModelProvider,
    provider_id: str,
    from_version: str,
    to_version: str,
    before: dict[str, Any],
    after: Observation,
    changed_files: list[str],
) -> None:
    """Ask what the failures mean. The verdict above does not change."""
    reports = [
        CheckReport(
            name=outcome.name,
            before_ok=bool(before.get(outcome.name) and before[outcome.name].ok),
            after_ok=outcome.ok,
            after_status=outcome.status or 0,
            after_reason=outcome.reason[:500],
        )
        for outcome in after.outcomes
    ]
    excerpts = [
        outcome.excerpt for outcome in after.outcomes if not outcome.ok and outcome.excerpt
    ][:MAX_EXCERPTS]

    agent = ReleaseGuardianAgent(model_provider)
    try:
        output: ReleaseGuardianOutput = await agent.run(
            ReleaseGuardianInput(
                provider_id=provider_id,
                from_version=from_version,
                to_version=to_version,
                checks=reports,
                changed_files=list(changed_files),
                response_excerpts=excerpts,
            )
        )
    except Exception as exc:
        logger.warning(
            "continuity.release_guardian_failed", extra={"error": type(exc).__name__}
        )
        assessment.rationale = (
            "The Release Guardian could not be consulted; this assessment is the "
            "check comparison alone."
        )
        return

    assessment.model_consulted = True
    assessment.likely_related_to_migration = output.likely_related_to_migration
    assessment.rollback_recommended = output.rollback_recommended
    assessment.rationale = redact(output.rationale)
    if output.regression_summary:
        # Appended rather than substituted. The counted facts stay first, and
        # the agent's reading follows them.
        assessment.summary = (
            f"{assessment.summary} {redact(output.regression_summary)}".strip()
        )

    if output.rollback_recommended:
        logger.warning(
            "continuity.rollback_recommended",
            extra={
                "provider_id": provider_id,
                "to_version": to_version,
                # Said in the log too, because a line saying "rollback" invites
                # the reading that one happened.
                "action_taken": "none",
            },
        )
