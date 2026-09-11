"""What happens after a person merges, and when the baseline may move.

The loop's last step, and the one that makes the next pass correct. Until the
baseline advances, Continuity still believes the project runs against the old
provider version — so the next release would be diffed from the wrong side.

Deliberately **deterministic and small**. A full Release Guardian — synthetic
integration checks against a live environment, regression comparison, rollback
*recommendation* — is Phase 9 (C9-02) and is not built. What is here verifies
the things that can be verified from what Continuity already knows:

* the pull request really merged (from the record the webhook or poller wrote);
* the migration it carried is the one Continuity produced (by branch and run);
* the provider version being adopted is the one the run migrated to.

**The baseline advances only on success.** A verification that could not run
leaves it where it is — "we could not check" is not "it is fine", and moving a
baseline on an unverified merge would hide the next real break.

This does not claim to have verified a deployed application. It says exactly
what it checked, and `VERIFIED` means "merged and consistent with the run",
which the evidence report states in those words.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import MigrationRun, Project, PullRequest, RunState
from backend.models.enums import ActivityEventKind
from backend.models.schemas import TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.providers.storage import advance_baseline, baseline_version

logger = get_logger(__name__)


@dataclass(slots=True)
class VerificationResult:
    """What post-merge verification checked, and what it concluded."""

    migration_run_id: uuid.UUID
    verified: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)
    baseline_before: str | None = None
    baseline_after: str | None = None

    @property
    def baseline_advanced(self) -> bool:
        return (
            self.baseline_after is not None
            and self.baseline_after != self.baseline_before
        )

    def summary(self) -> dict[str, Any]:
        return {
            "migration_run_id": str(self.migration_run_id),
            "verified": self.verified,
            "reason": self.reason,
            "checks": dict(self.checks),
            "baseline_before": self.baseline_before,
            "baseline_after": self.baseline_after,
            "baseline_advanced": self.baseline_advanced,
            # Said plainly, because the word invites a stronger reading than it
            # deserves.
            "scope": (
                "merge consistency only; no deployed application was contacted"
            ),
        }


async def verify_merge(
    session: AsyncSession, run: MigrationRun
) -> VerificationResult:
    """Check a merged run and, on success, advance the baseline."""
    project = await session.get(Project, run.project_id)
    if project is None:  # pragma: no cover - foreign key
        raise RuntimeError("the run has no project")

    before = await baseline_version(session, project, run.provider_id)
    result = VerificationResult(
        migration_run_id=run.id,
        verified=False,
        reason="",
        baseline_before=before,
        baseline_after=before,
    )

    await _move(session, run, RunState.POST_MERGE_VERIFICATION_RUNNING, "verifying the merge")

    pull = (
        await session.execute(
            select(PullRequest).where(PullRequest.migration_run_id == run.id)
        )
    ).scalar_one_or_none()

    result.checks = {
        "pull_request_recorded": pull is not None,
        "pull_request_merged": bool(pull and pull.merged),
        "branch_is_continuitys": bool(
            pull and pull.branch and pull.branch.startswith("continuity/migrate-")
        ),
        "branch_matches_run": bool(
            pull and run.target_branch and pull.branch == run.target_branch
        ),
        "run_has_target_version": bool(run.to_version),
    }

    failed = sorted(name for name, ok in result.checks.items() if not ok)

    if failed:
        result.reason = "verification failed: " + ", ".join(failed)
        await _move(
            session, run, RunState.POST_MERGE_VERIFICATION_FAILED, result.reason
        )
        await _move(
            session,
            run,
            RunState.HUMAN_REVIEW_REQUIRED,
            "post-merge verification did not pass",
        )
        logger.warning("continuity.post_merge_failed", extra=result.summary())
        return result

    result.verified = True
    result.reason = (
        f"pull request #{pull.number if pull else '?'} merged on "
        f"{run.target_branch}; consistent with this run"
    )

    await _move(session, run, RunState.POST_MERGE_VERIFICATION_PASSED, result.reason)

    # Only now. The baseline is Continuity's belief about what the project runs
    # against, and moving it on an unverified merge would make the next real
    # break invisible.
    await advance_baseline(session, project, run.provider_id, run.to_version)
    result.baseline_after = run.to_version

    await _move(session, run, RunState.VERIFIED, result.reason)
    await _return_to_monitoring(session, project)

    await events.emit(
        session,
        kind=ActivityEventKind.MIGRATION_VERIFIED,
        actor="post_merge",
        summary=(
            f"{run.provider_id} baseline advanced {before or 'unset'} → "
            f"{run.to_version} after the merge."
        ),
        project_id=project.id,
        migration_run_id=run.id,
    )

    logger.info("continuity.post_merge_verified", extra=result.summary())
    return result


async def verify_merged_runs(session: AsyncSession) -> list[VerificationResult]:
    """Verify every run whose pull request has merged and which is still waiting.

    The scheduler calls this after its sweep, so a merge detected by a webhook
    or by polling is followed up without anyone asking.
    """
    runs = list(
        (
            await session.execute(
                select(MigrationRun)
                .join(PullRequest, PullRequest.migration_run_id == MigrationRun.id)
                .where(
                    PullRequest.merged.is_(True),
                    MigrationRun.state == RunState.MERGE_WAITING,
                )
            )
        ).scalars()
    )

    results = []
    for run in runs:
        try:
            results.append(await verify_merge(session, run))
        except Exception as exc:
            # One run's failure must not stop the others being verified.
            logger.warning(
                "continuity.post_merge_error",
                extra={"migration_run_id": str(run.id), "error": type(exc).__name__},
            )
    return results


async def _return_to_monitoring(session: AsyncSession, project: Project) -> None:
    tracked = await session.get(Project, project.id) or project
    if not can_transition(tracked.state, RunState.MONITORING_ACTIVE):
        return
    await transition(
        session,
        from_state=tracked.state,
        to_state=RunState.MONITORING_ACTIVE,
        evidence=TransitionEvidence(
            reason="migration verified; monitoring resumed",
            actor="post_merge",
            detail={},
        ),
        project_id=tracked.id,
    )
    tracked.state = RunState.MONITORING_ACTIVE
    await session.flush()


async def _move(
    session: AsyncSession, run: MigrationRun, to_state: RunState, reason: str
) -> None:
    tracked = await session.get(MigrationRun, run.id) or run
    if not can_transition(tracked.state, to_state):
        return
    await transition(
        session,
        from_state=tracked.state,
        to_state=to_state,
        evidence=TransitionEvidence(reason=reason, actor="post_merge", detail={}),
        project_id=tracked.project_id,
        migration_run_id=tracked.id,
    )
    tracked.state = to_state
    await session.flush()
