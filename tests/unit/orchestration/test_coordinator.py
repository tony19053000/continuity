"""C2-05 acceptance: the coordinator drives runs without owning policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.approvals.service import ApprovalRequest, resolve
from backend.models import (
    Approval,
    ApprovalStatus,
    ChangeEvent,
    ChangeType,
    MigrationAttempt,
    MigrationRun,
    Project,
    Repository,
    RunState,
    Severity,
    StateTransition,
    User,
)
from backend.models.session import session_scope
from backend.orchestration.coordinator import RunCoordinator
from backend.shared.errors import IllegalTransition

MAX_ATTEMPTS = 3


@pytest.fixture
def coordinator() -> RunCoordinator:
    return RunCoordinator(max_repair_attempts=MAX_ATTEMPTS)


async def _make_run(session: AsyncSession, state: RunState) -> MigrationRun:
    user = User(google_subject=f"sub-{uuid.uuid4()}", email="c@example.test")
    session.add(user)
    await session.flush()

    repository = Repository(owner="acme", name=f"repo-{uuid.uuid4().hex[:8]}")
    session.add(repository)
    await session.flush()

    project = Project(user_id=user.id, repository_id=repository.id, name="commerce-api")
    session.add(project)
    await session.flush()

    change = ChangeEvent(
        provider_id="payments",
        old_version="v1",
        new_version="v2",
        change_type=ChangeType.WEBHOOK_EVENT_CHANGED,
        resource=f"payment.paid.{uuid.uuid4().hex[:6]}",
        breaking=True,
        source={"kind": "changelog"},
        evidence={"kind": "changelog", "confidence": "confirmed"},
        detected_at=datetime.now(UTC),
    )
    session.add(change)
    await session.flush()

    run = MigrationRun(
        project_id=project.id,
        change_event_id=change.id,
        provider_id="payments",
        from_version="v1",
        to_version="v2",
        state=state,
    )
    session.add(run)
    await session.flush()
    return run


# --- Transitions ---------------------------------------------------------


async def test_advancing_records_the_transition_and_updates_the_run(
    database: None, coordinator: RunCoordinator
) -> None:
    async with session_scope() as session:
        run = await _make_run(session, RunState.VALIDATION_RUNNING)
        await coordinator.advance(
            session,
            run,
            to_state=RunState.VALIDATION_PASSED,
            reason="47/47 passed",
            actor="validator",
        )
        run_id = run.id

    async with session_scope() as session:
        reloaded = await session.get(MigrationRun, run_id)
        transitions = (
            await session.execute(
                select(StateTransition).where(StateTransition.migration_run_id == run_id)
            )
        ).scalars().all()

    assert reloaded is not None
    assert reloaded.state is RunState.VALIDATION_PASSED
    assert len(transitions) == 1
    assert transitions[0].to_state is RunState.VALIDATION_PASSED


async def test_an_illegal_advance_is_refused(
    database: None, coordinator: RunCoordinator
) -> None:
    with pytest.raises(IllegalTransition):
        async with session_scope() as session:
            run = await _make_run(session, RunState.MIGRATION_PENDING)
            await coordinator.advance(
                session, run, to_state=RunState.PR_CREATED, reason="skip", actor="orchestrator"
            )


# --- Model proposals -----------------------------------------------------


async def test_a_legal_proposal_is_applied(
    database: None, coordinator: RunCoordinator
) -> None:
    async with session_scope() as session:
        run = await _make_run(session, RunState.PATCH_READY)
        await coordinator.apply_proposal(
            session, run, proposed_state="validation_running", rationale="patch is ready"
        )

        assert run.state is RunState.VALIDATION_RUNNING


async def test_an_illegal_proposal_escalates_instead_of_moving_the_run(
    database: None, coordinator: RunCoordinator
) -> None:
    """The concrete point where model output is refused as workflow control.

    The Orchestrator asking to skip straight to a pull request does not skip
    anything — it stops the run for a human.
    """
    async with session_scope() as session:
        run = await _make_run(session, RunState.MIGRATION_RUNNING)
        await coordinator.apply_proposal(
            session, run, proposed_state="pr_created", rationale="looks fine to me"
        )

        assert run.state is RunState.HUMAN_REVIEW_REQUIRED


async def test_a_proposal_naming_an_unknown_state_escalates(
    database: None, coordinator: RunCoordinator
) -> None:
    async with session_scope() as session:
        run = await _make_run(session, RunState.MIGRATION_RUNNING)
        await coordinator.apply_proposal(
            session, run, proposed_state="everything_is_fine", rationale="trust me"
        )

        assert run.state is RunState.HUMAN_REVIEW_REQUIRED


# --- Repair budget -------------------------------------------------------


async def test_validation_failure_routes_to_repair_while_budget_remains(
    database: None, coordinator: RunCoordinator
) -> None:
    async with session_scope() as session:
        run = await _make_run(session, RunState.VALIDATION_FAILED)
        session.add(MigrationAttempt(migration_run_id=run.id, attempt_number=1))
        await session.flush()

        await coordinator.route_validation_failure(session, run, failure_summary="4 failed")

        assert run.state is RunState.REPAIR_RUNNING


async def test_an_exhausted_budget_escalates_rather_than_looping(
    database: None, coordinator: RunCoordinator
) -> None:
    async with session_scope() as session:
        run = await _make_run(session, RunState.VALIDATION_FAILED)
        for number in range(1, MAX_ATTEMPTS + 1):
            session.add(MigrationAttempt(migration_run_id=run.id, attempt_number=number))
        await session.flush()

        await coordinator.route_validation_failure(session, run, failure_summary="still failing")

        assert run.state is RunState.HUMAN_REVIEW_REQUIRED


async def test_the_budget_is_counted_from_persisted_rows(
    database: None, coordinator: RunCoordinator
) -> None:
    """A restart must not hand the loop a fresh budget."""
    async with session_scope() as session:
        run = await _make_run(session, RunState.VALIDATION_FAILED)
        session.add(MigrationAttempt(migration_run_id=run.id, attempt_number=1))
        session.add(MigrationAttempt(migration_run_id=run.id, attempt_number=2))
        await session.flush()
        run_id = run.id

    # A fresh coordinator, as after a process restart.
    async with session_scope() as session:
        reloaded = await session.get(MigrationRun, run_id)
        assert reloaded is not None
        used = await RunCoordinator(max_repair_attempts=MAX_ATTEMPTS).attempts_used(
            session, reloaded
        )

    assert used == 2


# --- Approval pause and resume -------------------------------------------


async def test_a_run_pauses_on_approval_and_resumes_after_a_human_decides(
    database: None, coordinator: RunCoordinator
) -> None:
    async with session_scope() as session:
        run = await _make_run(session, RunState.SECURITY_REVIEW_RUNNING)
        pause = await coordinator.pause_for_approval(
            session,
            run,
            ApprovalRequest(
                project_id=run.project_id,
                trigger="oauth_scope_expansion",
                risk=Severity.HIGH,
                requested_action={"from": "customers.read", "to": "customers.write"},
                migration_run_id=run.id,
            ),
        )
        run_id, approval_id, user_id = run.id, pause.approval_id, run.project_id

        assert run.state is RunState.APPROVAL_PENDING

    # The pause is persisted, so it survives a restart.
    async with session_scope() as session:
        reloaded = await session.get(MigrationRun, run_id)
        assert reloaded is not None
        assert reloaded.state is RunState.APPROVAL_PENDING

    async with session_scope() as session:
        approver = (await session.execute(select(User))).scalars().first()
        assert approver is not None
        await resolve(session, approval_id, actor=approver, approved=True)

    async with session_scope() as session:
        reloaded = await session.get(MigrationRun, run_id)
        assert reloaded is not None
        await coordinator.resume_after_approval(session, reloaded, approval_id)

        assert reloaded.state is RunState.APPROVED
    assert user_id


async def test_a_rejected_approval_stops_the_run(
    database: None, coordinator: RunCoordinator
) -> None:
    async with session_scope() as session:
        run = await _make_run(session, RunState.SECURITY_REVIEW_RUNNING)
        pause = await coordinator.pause_for_approval(
            session,
            run,
            ApprovalRequest(
                project_id=run.project_id,
                trigger="oauth_scope_expansion",
                risk=Severity.HIGH,
                requested_action={},
                migration_run_id=run.id,
            ),
        )
        run_id, approval_id = run.id, pause.approval_id

    async with session_scope() as session:
        approver = (await session.execute(select(User))).scalars().first()
        assert approver is not None
        await resolve(session, approval_id, actor=approver, approved=False)

    async with session_scope() as session:
        reloaded = await session.get(MigrationRun, run_id)
        assert reloaded is not None
        await coordinator.resume_after_approval(session, reloaded, approval_id)

        assert reloaded.state is RunState.REJECTED


async def test_a_run_cannot_resume_while_its_approval_is_still_pending(
    database: None, coordinator: RunCoordinator
) -> None:
    """The re-check catches a resume attempted before a human decided."""
    async with session_scope() as session:
        run = await _make_run(session, RunState.SECURITY_REVIEW_RUNNING)
        pause = await coordinator.pause_for_approval(
            session,
            run,
            ApprovalRequest(
                project_id=run.project_id,
                trigger="production_operation",
                risk=Severity.HIGH,
                requested_action={},
                migration_run_id=run.id,
            ),
        )

        with pytest.raises(IllegalTransition):
            await coordinator.resume_after_approval(session, run, pause.approval_id)


async def test_an_approval_cannot_be_resolved_twice(database: None) -> None:
    """A rejected request must not be quietly re-approved."""
    from backend.approvals.service import ApprovalAlreadyResolved, create_request

    async with session_scope() as session:
        run = await _make_run(session, RunState.SECURITY_REVIEW_RUNNING)
        approval = await create_request(
            session,
            ApprovalRequest(
                project_id=run.project_id,
                trigger="weaken_test",
                risk=Severity.HIGH,
                requested_action={},
                migration_run_id=run.id,
            ),
        )
        approval_id = approval.id

    async with session_scope() as session:
        approver = (await session.execute(select(User))).scalars().first()
        assert approver is not None
        await resolve(session, approval_id, actor=approver, approved=False)

    async with session_scope() as session:
        approver = (await session.execute(select(User))).scalars().first()
        assert approver is not None
        with pytest.raises(ApprovalAlreadyResolved):
            await resolve(session, approval_id, actor=approver, approved=True)

    async with session_scope() as session:
        stored = await session.get(Approval, approval_id)

    assert stored is not None
    assert stored.status is ApprovalStatus.REJECTED
