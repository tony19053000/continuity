"""Continuing the same run after a person answers.

A migration that stops at `APPROVAL_PENDING` has already been written,
validated, and reviewed. The workspace holding it is then destroyed — it has to
be, or every paused run would hold a git worktree until someone got round to it.
So resumption is not "carry on where we left off" but "rebuild exactly what was
validated, prove it is the same, and deliver that".

Three things make that safe rather than hopeful:

* **The patch is stored, not the workspace.** `migration_attempts.patch_diff`
  holds the diff the validator passed, and the run records the commit it was
  built from.
* **Rebuilding is verified, not assumed.** The diff is applied to a fresh
  worktree at the same commit, and the resulting tree's digest is compared with
  the one recorded before the pause. A mismatch refuses delivery.
* **The approval is re-read at the moment it is used.** A person can change
  their mind between granting and this running, and `require_granted` inside
  `deliver()` is what notices.

Rejection terminates: the run escalates to human review and nothing is
delivered. Nothing here can approve anything — this module reads approval state
and never writes it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.github.delivery import DeliveredPullRequest, DeliveryRefused, deliver
from backend.migrations.evidence import build_report
from backend.migrations.workspace import MigrationWorkspace, WorkspaceManager
from backend.models import Approval, MigrationAttempt, MigrationRun, Repository, RunState
from backend.models.enums import ApprovalStatus, AttemptOutcome
from backend.models.schemas import TransitionEvidence
from backend.observability.logging import get_logger
from backend.orchestration.delivery_gate import preconditions_for
from backend.orchestration.state_machine import can_transition, transition
from backend.shared.errors import ContinuityError

logger = get_logger(__name__)


class CannotResume(ContinuityError):
    """The run cannot be continued from its recorded state.

    Always a refusal to act, never a partial attempt: a run that cannot be
    rebuilt faithfully must not be delivered approximately.
    """

    code = "cannot_resume_run"
    status_code = 409
    message = "This migration run cannot be resumed."


@dataclass(slots=True)
class ResumeResult:
    migration_run_id: uuid.UUID
    final_state: RunState
    reason: str
    delivered: DeliveredPullRequest | None = None

    @property
    def reached_delivery(self) -> bool:
        return self.delivered is not None

    def summary(self) -> dict[str, Any]:
        return {
            "migration_run_id": str(self.migration_run_id),
            "final_state": self.final_state.value,
            "reason": self.reason,
            "pull_request": self.delivered.number if self.delivered else None,
        }


async def resume_run(
    session: AsyncSession,
    run: MigrationRun,
    *,
    workspaces: WorkspaceManager,
    github_client: Any | None = None,
    default_branch: str = "main",
) -> ResumeResult:
    """Continue one paused run, once its approvals are decided."""
    if run.state is not RunState.APPROVAL_PENDING:
        raise CannotResume(
            f"run {run.id} is {run.state.value}, not awaiting approval"
        )

    approvals = list(
        (
            await session.execute(
                select(Approval).where(Approval.migration_run_id == run.id)
            )
        ).scalars()
    )

    if not approvals:
        raise CannotResume("no approval was ever requested for this run")

    if any(a.status is ApprovalStatus.PENDING for a in approvals):
        return ResumeResult(
            migration_run_id=run.id,
            final_state=run.state,
            reason="still waiting on a human decision",
        )

    if any(a.status is ApprovalStatus.REJECTED for a in approvals):
        return await _reject(session, run)

    return await _approved(
        session,
        run,
        workspaces=workspaces,
        github_client=github_client,
        default_branch=default_branch,
    )


async def _reject(session: AsyncSession, run: MigrationRun) -> ResumeResult:
    """A rejected migration ends. It is not retried and not delivered."""
    await _move(
        session,
        run,
        RunState.REJECTED,
        "a human rejected this migration",
    )
    # REJECTED leads back to monitoring, which is the right shape: a refused
    # migration is an answer, not a failure, and the provider change stays
    # recorded so nothing re-detects it.
    await _move(
        session,
        run,
        RunState.MONITORING_ACTIVE,
        "rejected; returning to monitoring",
    )
    logger.info("continuity.run_rejected", extra={"migration_run_id": str(run.id)})
    return ResumeResult(
        migration_run_id=run.id,
        final_state=run.state,
        reason="rejected by a human; nothing was delivered",
    )


async def _approved(
    session: AsyncSession,
    run: MigrationRun,
    *,
    workspaces: WorkspaceManager,
    github_client: Any | None,
    default_branch: str,
) -> ResumeResult:
    """Rebuild the validated patch and deliver it."""
    await _move(session, run, RunState.APPROVED, "approved by a human")

    diff = await _stored_patch(session, run)

    if github_client is None:
        return ResumeResult(
            migration_run_id=run.id,
            final_state=run.state,
            reason="approved; no GitHub client is configured for this deployment",
        )

    repository = await session.get(Repository, (await _project_repo(session, run)))
    if repository is None:  # pragma: no cover - foreign key
        raise CannotResume("the project has no repository record")

    async with workspaces.open(
        run_id=run.id,
        source_commit=run.source_commit,
        target_branch=run.target_branch
        or f"continuity/migrate-{run.provider_id}-{run.to_version}",
    ) as workspace:
        await _apply(workspace, diff)
        changed = await workspace.changed_files()
        files = {path: workspace.read_file(path) for path in changed}

        # Proves the rebuilt tree is the reviewed one. `preconditions_for`
        # compares this against the digest recorded before the pause and
        # refuses on any difference.
        preconditions = await preconditions_for(session, run, files=files)

        # APPROVED does not reach PR_PENDING directly. The machine routes an
        # approved run back through final validation first, which is the right
        # shape — the approval was for a patch, and this is the last point
        # before it becomes a pull request.
        for state, reason in (
            (RunState.FINAL_VALIDATION_RUNNING, "re-checking the approved patch"),
            (RunState.FINAL_VALIDATION_PASSED, "the approved patch rebuilt cleanly"),
            (RunState.PR_PENDING, "approved; opening a pull request"),
        ):
            await _move(session, run, state, reason)
        report = await build_report(session, run)

        try:
            delivered = await deliver(
                session,
                run,
                repository,
                client=github_client,
                preconditions=preconditions,
                default_branch=default_branch,
                files=files,
                title=(
                    f"Migrate {run.provider_id} {run.from_version} "
                    f"→ {run.to_version}"
                ),
                body=report.as_markdown(),
                commit_message=(
                    f"migrate {run.provider_id} to {run.to_version}\n\n"
                    "Opened by Continuity after human approval. Evidence is in "
                    "the pull request body."
                ),
            )
        except DeliveryRefused as refusal:
            return ResumeResult(
                migration_run_id=run.id,
                final_state=run.state,
                reason=str(refusal),
            )

    refreshed = await session.get(MigrationRun, run.id)
    logger.info(
        "continuity.run_resumed",
        extra={"migration_run_id": str(run.id), "number": delivered.number},
    )
    return ResumeResult(
        migration_run_id=run.id,
        final_state=refreshed.state if refreshed else RunState.MERGE_WAITING,
        reason=f"approved and delivered as pull request #{delivered.number}",
        delivered=delivered,
    )


async def _stored_patch(session: AsyncSession, run: MigrationRun) -> str:
    """The diff the validator passed. Refuses if there is not exactly one."""
    attempts = list(
        (
            await session.execute(
                select(MigrationAttempt)
                .where(
                    MigrationAttempt.migration_run_id == run.id,
                    MigrationAttempt.outcome == AttemptOutcome.PASSED,
                )
                .order_by(MigrationAttempt.attempt_number.desc())
            )
        ).scalars()
    )

    if not attempts:
        raise CannotResume("no attempt on this run ever passed validation")

    diff = attempts[0].patch_diff
    if not diff or not diff.strip():
        raise CannotResume("the passing attempt recorded no patch to re-apply")
    return diff


async def _project_repo(session: AsyncSession, run: MigrationRun) -> uuid.UUID:
    from backend.models import Project

    project = await session.get(Project, run.project_id)
    if project is None:  # pragma: no cover - foreign key
        raise CannotResume("the run has no project")
    return project.repository_id


async def _apply(workspace: MigrationWorkspace, diff: str) -> None:
    """Re-apply the stored diff to a fresh worktree.

    `git apply` rather than rewriting files from a parsed diff: it either
    applies cleanly or it fails, and a patch that no longer applies must stop
    the run rather than be approximated.
    """
    patch_path = workspace.resolve(".continuity-resume.patch")
    patch_path.write_text(diff)

    result = await workspace.run(["git", "apply", "--whitespace=nowarn", str(patch_path.name)])
    patch_path.unlink(missing_ok=True)

    if not result.ok:
        raise CannotResume(
            "the approved patch no longer applies to its source commit: "
            f"{result.stderr.strip()[:300]}"
        )


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
        evidence=TransitionEvidence(reason=reason, actor="approval", detail={}),
        project_id=tracked.project_id,
        migration_run_id=tracked.id,
    )
    tracked.state = to_state
    await session.flush()


async def resume_decided_runs(
    session: AsyncSession,
    *,
    workspaces_for: Any,
    github_for: Any,
    default_branch: str = "main",
) -> list[ResumeResult]:
    """Continue every paused run whose approvals have been answered.

    Called by the scheduler after each sweep, so approving in the UI is enough
    — nobody has to poke the run afterwards. A run still waiting is skipped
    silently, because waiting is the normal state of a paused run.

    `workspaces_for` and `github_for` are callables taking a project id, so this
    does not need to know how a deployment builds either.
    """
    paused = list(
        (
            await session.execute(
                select(MigrationRun).where(
                    MigrationRun.state == RunState.APPROVAL_PENDING
                )
            )
        ).scalars()
    )

    results: list[ResumeResult] = []
    for run in paused:
        decided = await _approvals_decided(session, run)
        if not decided:
            continue

        workspaces = await workspaces_for(run.project_id)
        if workspaces is None:
            logger.info(
                "continuity.resume_deferred",
                extra={
                    "migration_run_id": str(run.id),
                    "reason": "no local checkout to rebuild the patch in",
                },
            )
            continue

        try:
            results.append(
                await resume_run(
                    session,
                    run,
                    workspaces=workspaces,
                    github_client=await github_for(run.project_id),
                    default_branch=default_branch,
                )
            )
        except Exception as exc:
            # One run's failure must not stop the others being resumed.
            logger.warning(
                "continuity.resume_failed",
                extra={"migration_run_id": str(run.id), "error": type(exc).__name__},
            )

    return results


async def _approvals_decided(session: AsyncSession, run: MigrationRun) -> bool:
    statuses = list(
        (
            await session.execute(
                select(Approval.status).where(Approval.migration_run_id == run.id)
            )
        ).scalars()
    )
    return bool(statuses) and all(
        status is not ApprovalStatus.PENDING for status in statuses
    )
