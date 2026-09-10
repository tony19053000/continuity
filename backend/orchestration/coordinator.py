"""Run coordination.

The coordinator sequences agents and moves runs. It is ordinary code, and that
is the point: the Orchestrator agent may *propose* a next state, but only this
module — checking `ALLOWED_TRANSITIONS` — can cause one.

Three guarantees live here:

* **Retry budgets are finite** and enforced by counting persisted rows, so a
  restart cannot reset a budget mid-run.
* **A run pauses on ASK** and resumes only when a stored approval says APPROVED.
  Pausing is persisted state, so it survives a process restart.
* **A model proposal is validated before it is acted on.** A proposal naming an
  illegal or unknown state is refused and the run escalates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.approvals.service import ApprovalRequest, create_request, is_approved
from backend.models import (
    ActivityEventKind,
    Approval,
    ApprovalStatus,
    MigrationAttempt,
    MigrationRun,
    RunState,
)
from backend.models.schemas import TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.shared.errors import ContinuityError, IllegalTransition

logger = get_logger(__name__)


class RetryBudgetExhausted(ContinuityError):
    """The repair loop hit `MAX_REPAIR_ATTEMPTS`.

    Not an error in the system — a deliberate stop. The run moves to
    `HUMAN_REVIEW_REQUIRED` rather than looping.
    """

    code = "retry_budget_exhausted"
    status_code = 409
    message = "The repair budget for this migration is exhausted."


@dataclass(frozen=True, slots=True)
class PauseResult:
    """A run stopped to wait for a person."""

    approval_id: uuid.UUID
    reason: str


class RunCoordinator:
    """Drives one migration run through the state machine."""

    def __init__(self, *, max_repair_attempts: int) -> None:
        self._max_repair_attempts = max_repair_attempts

    async def advance(
        self,
        session: AsyncSession,
        run: MigrationRun,
        *,
        to_state: RunState,
        reason: str,
        actor: str,
        detail: dict[str, object] | None = None,
    ) -> MigrationRun:
        """Move a run, recording the transition and its evidence.

        The run's `state` column and the `state_transitions` row are written in
        the same transaction, so the recorded history and the current state can
        never disagree.
        """
        await transition(
            session,
            from_state=run.state,
            to_state=to_state,
            evidence=TransitionEvidence(reason=reason, actor=actor, detail=detail or {}),
            project_id=run.project_id,
            migration_run_id=run.id,
        )
        run.state = to_state
        await session.flush()
        return run

    async def apply_proposal(
        self,
        session: AsyncSession,
        run: MigrationRun,
        *,
        proposed_state: str,
        rationale: str,
    ) -> MigrationRun:
        """Act on the Orchestrator agent's proposal, after validating it.

        A model naming a state that does not exist, or one not reachable from
        here, does not move the run — it escalates. This is the concrete point
        where "LLM output never equals authorization" is enforced for workflow
        control.
        """
        try:
            target = RunState(proposed_state)
        except ValueError:
            logger.warning(
                "continuity.invalid_state_proposal",
                extra={"migration_run_id": str(run.id), "proposed": proposed_state},
            )
            return await self.escalate(
                session, run, reason=f"Orchestrator proposed unknown state {proposed_state!r}."
            )

        if not can_transition(run.state, target):
            logger.warning(
                "continuity.illegal_state_proposal",
                extra={
                    "migration_run_id": str(run.id),
                    "from": run.state.value,
                    "proposed": target.value,
                },
            )
            return await self.escalate(
                session,
                run,
                reason=(
                    f"Orchestrator proposed an illegal transition "
                    f"{run.state.value} -> {target.value}."
                ),
            )

        return await self.advance(
            session, run, to_state=target, reason=rationale, actor="orchestrator"
        )

    # --- Repair budget ---------------------------------------------------

    async def attempts_used(self, session: AsyncSession, run: MigrationRun) -> int:
        """Attempts recorded so far.

        Counted from persisted rows rather than held in memory, so a restart
        mid-run cannot hand the loop a fresh budget.
        """
        return (
            await session.execute(
                select(func.count())
                .select_from(MigrationAttempt)
                .where(MigrationAttempt.migration_run_id == run.id)
            )
        ).scalar_one()

    async def may_repair(self, session: AsyncSession, run: MigrationRun) -> bool:
        return await self.attempts_used(session, run) < self._max_repair_attempts

    async def route_validation_failure(
        self, session: AsyncSession, run: MigrationRun, *, failure_summary: str
    ) -> MigrationRun:
        """Send a failed validation back for repair, or stop.

        The branch is decided here, by counting rows — never by the model that
        just failed.
        """
        if await self.may_repair(session, run):
            await events.emit(
                session,
                kind=ActivityEventKind.REPAIR_STARTED,
                actor="orchestrator",
                summary=f"Validation failed; repairing. {failure_summary}",
                project_id=run.project_id,
                migration_run_id=run.id,
            )
            return await self.advance(
                session,
                run,
                to_state=RunState.REPAIR_RUNNING,
                reason=failure_summary,
                actor="orchestrator",
                detail={"attempts_used": await self.attempts_used(session, run)},
            )

        return await self.escalate(
            session,
            run,
            reason=(
                f"Repair budget of {self._max_repair_attempts} attempts is exhausted. "
                f"Last failure: {failure_summary}"
            ),
        )

    async def escalate(
        self, session: AsyncSession, run: MigrationRun, *, reason: str
    ) -> MigrationRun:
        """Stop deliberately and hand the run to a person."""
        return await self.advance(
            session,
            run,
            to_state=RunState.HUMAN_REVIEW_REQUIRED,
            reason=reason,
            actor="orchestrator",
        )

    # --- Approval pause and resume ---------------------------------------

    async def pause_for_approval(
        self, session: AsyncSession, run: MigrationRun, request: ApprovalRequest
    ) -> PauseResult:
        """Record an approval request and stop the run.

        The pause is persisted state, not an in-memory wait, so the run is still
        paused after a restart and resumes only on a human decision.
        """
        approval = await create_request(session, request)

        await events.emit(
            session,
            kind=ActivityEventKind.APPROVAL_REQUIRED,
            actor="orchestrator",
            summary=f"Waiting for approval: {request.trigger}",
            project_id=run.project_id,
            migration_run_id=run.id,
        )
        await self.advance(
            session,
            run,
            to_state=RunState.APPROVAL_PENDING,
            reason=f"Approval required: {request.trigger}",
            actor="orchestrator",
            detail={"approval_id": str(approval.id), "risk": request.risk.value},
        )
        return PauseResult(approval_id=approval.id, reason=request.trigger)

    async def resume_after_approval(
        self, session: AsyncSession, run: MigrationRun, approval_id: uuid.UUID
    ) -> MigrationRun:
        """Continue a paused run, re-checking the stored decision first.

        The state is re-read here rather than trusted from earlier in the run:
        an approval can be rejected between the pause and the resume.
        """
        if run.state is not RunState.APPROVAL_PENDING:
            raise IllegalTransition(run.state.value, RunState.APPROVED.value)

        approval = await session.get(Approval, approval_id)
        if approval is None or approval.status is ApprovalStatus.PENDING:
            raise IllegalTransition(run.state.value, RunState.APPROVED.value)

        if not await is_approved(session, approval_id):
            return await self.advance(
                session,
                run,
                to_state=RunState.REJECTED,
                reason="The approval request was rejected.",
                actor=str(approval.actor_user_id),
            )

        await events.emit(
            session,
            kind=ActivityEventKind.APPROVAL_RECEIVED,
            actor=str(approval.actor_user_id),
            summary=f"Approved: {approval.trigger}",
            project_id=run.project_id,
            migration_run_id=run.id,
        )
        return await self.advance(
            session,
            run,
            to_state=RunState.APPROVED,
            reason=f"Approved by {approval.actor_user_id}.",
            actor=str(approval.actor_user_id),
        )
