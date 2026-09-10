"""C3-06 acceptance: the scan drives the documented states and emits documented events."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.models import (
    ActivityEvent,
    ActivityEventKind,
    Project,
    Repository,
    RunState,
    StateTransition,
    User,
)
from backend.models.session import session_scope
from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.workers.repository_scan import run_repository_scan


async def _make_project(session, state: RunState = RunState.INITIAL_SCAN_PENDING) -> Project:
    user = User(google_subject=f"sub-{uuid.uuid4()}", email="scan@example.test")
    session.add(user)
    await session.flush()

    repository = Repository(owner="acme", name=f"repo-{uuid.uuid4().hex[:8]}")
    session.add(repository)
    await session.flush()

    project = Project(
        user_id=user.id, repository_id=repository.id, name="commerce-api", state=state
    )
    session.add(project)
    await session.flush()
    return project


async def test_a_scan_reaches_initial_scan_complete(database: None, sample_repo: Path) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        result = await run_repository_scan(
            session, project, LocalRepositoryAdapter(sample_repo)
        )
        project_id = project.id

        assert project.state is RunState.INITIAL_SCAN_COMPLETE
        assert result.summary["files_indexed"] == 7

    async with session_scope() as session:
        reloaded = await session.get(Project, project_id)

    assert reloaded is not None
    assert reloaded.state is RunState.INITIAL_SCAN_COMPLETE
    assert reloaded.last_scanned_at is not None


async def test_the_transitions_match_the_documented_sequence(
    database: None, sample_repo: Path
) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        await run_repository_scan(session, project, LocalRepositoryAdapter(sample_repo))
        project_id = project.id

    async with session_scope() as session:
        transitions = (
            await session.execute(
                select(StateTransition)
                .where(StateTransition.project_id == project_id)
                .order_by(StateTransition.occurred_at)
            )
        ).scalars().all()

    assert [(t.from_state, t.to_state) for t in transitions] == [
        (RunState.INITIAL_SCAN_PENDING, RunState.INITIAL_SCAN_RUNNING),
        (RunState.INITIAL_SCAN_RUNNING, RunState.INITIAL_SCAN_COMPLETE),
    ]


async def test_emitted_events_come_from_the_documented_vocabulary(
    database: None, sample_repo: Path
) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        await run_repository_scan(session, project, LocalRepositoryAdapter(sample_repo))
        project_id = project.id

    async with session_scope() as session:
        emitted = (
            await session.execute(
                select(ActivityEvent).where(ActivityEvent.project_id == project_id)
            )
        ).scalars().all()

    kinds = {event.kind for event in emitted}
    assert ActivityEventKind.REPOSITORY_SCAN_STARTED in kinds
    assert ActivityEventKind.INTEGRATION_DETECTED in kinds
    # Typing the column with the enum means an undocumented name cannot persist.
    assert all(isinstance(event.kind, ActivityEventKind) for event in emitted)


async def test_the_summary_records_what_was_refused(
    database: None, sample_repo: Path
) -> None:
    """The scan reports the secrets it declined to open, not silence."""
    async with session_scope() as session:
        project = await _make_project(session)
        result = await run_repository_scan(
            session, project, LocalRepositoryAdapter(sample_repo)
        )

    assert result.summary["secret_paths_excluded"] == 2
    assert result.summary["files_excluded"] == 4


async def test_a_failing_scan_moves_the_run_to_failed_rather_than_leaving_it_running(
    database: None, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partially-indexed project must never look complete."""
    import backend.workers.repository_scan as scan_module

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("indexer exploded")

    monkeypatch.setattr(scan_module, "build_index", explode)

    async with session_scope() as session:
        project = await _make_project(session)

        with pytest.raises(RuntimeError):
            await run_repository_scan(session, project, LocalRepositoryAdapter(sample_repo))

        assert project.state is RunState.RUN_FAILED


async def test_scanning_an_empty_repository_is_a_valid_result(
    database: None, tmp_path: Path
) -> None:
    """No integrations found is an answer, not an error."""
    (tmp_path / "README.md").write_text("# empty\n")

    async with session_scope() as session:
        project = await _make_project(session)
        result = await run_repository_scan(
            session, project, LocalRepositoryAdapter(tmp_path)
        )

        assert project.state is RunState.INITIAL_SCAN_COMPLETE
        assert result.summary["dependencies"] == 0
        assert result.summary["call_sites"] == 0
