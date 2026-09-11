"""B-06: the loop that makes Continuity autonomous rather than merely automatic.

Every stage could already run; nothing ran them. These tests are about the
properties that make an unattended loop safe to leave running: it does not
overlap itself, one project's failure does not stop the sweep, and shutdown does
not orphan work.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from backend.models import Project, Repository, RunState, User
from backend.models.session import session_scope
from backend.orchestration.pipeline import PipelineResult
from backend.workers.scheduler import MONITORABLE, ProviderScheduler

pytestmark = pytest.mark.usefixtures("database")


async def _project(state: RunState = RunState.MONITORING_ACTIVE) -> Project:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="s@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(
            user_id=user.id, repository_id=repository.id, name="p", state=state
        )
        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project


class RecordingPipeline:
    """Stands in for `run_pipeline`, recording what it was asked to do."""

    def __init__(self, *, delay: float = 0.0, explode: bool = False) -> None:
        self.calls: list[uuid.UUID] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._delay = delay
        self._explode = explode

    async def __call__(self, session: Any, project: Project, **_: Any) -> PipelineResult:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.calls.append(project.id)
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._explode:
                raise RuntimeError("the pipeline fell over")
            return PipelineResult(project_id=project.id, stopped_at="done")
        finally:
            self.concurrent -= 1


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch):
    def install(pipeline: RecordingPipeline) -> RecordingPipeline:
        import backend.workers.scheduler as scheduler_module

        monkeypatch.setattr(scheduler_module, "run_pipeline", pipeline)
        return pipeline

    return install


async def _collaborators(project_id: uuid.UUID) -> dict[str, Any]:
    return {}


def _scheduler(**kwargs: Any) -> ProviderScheduler:
    kwargs.setdefault("interval_seconds", 1)
    kwargs.setdefault("pipeline_factory", _collaborators)
    return ProviderScheduler(**kwargs)


# --- what a tick does -----------------------------------------------------


async def test_a_tick_runs_the_pipeline_for_each_monitorable_project(
    patched_pipeline: Any,
) -> None:
    pipeline = patched_pipeline(RecordingPipeline())
    first = await _project()
    second = await _project()

    result = await _scheduler().tick()

    assert sorted(pipeline.calls) == sorted([first.id, second.id])
    assert sorted(result.checked) == sorted([first.id, second.id])
    assert result.errors == {}


@pytest.mark.parametrize(
    "state",
    [
        RunState.MIGRATION_RUNNING,
        RunState.REPAIR_RUNNING,
        RunState.MERGE_WAITING,
        RunState.HUMAN_REVIEW_REQUIRED,
        RunState.PROJECT_CREATED,
    ],
)
async def test_a_project_mid_flight_is_left_alone(
    state: RunState, patched_pipeline: Any
) -> None:
    """Starting a second pass on a busy project would open a competing run.

    `HUMAN_REVIEW_REQUIRED` is here for a different reason: a run waiting on a
    person must not be quietly restarted around them.
    """
    pipeline = patched_pipeline(RecordingPipeline())
    project = await _project(state)

    result = await _scheduler().tick()

    assert pipeline.calls == []
    assert result.skipped[str(project.id)] == f"state is {state.value}"


@pytest.mark.parametrize("state", sorted(MONITORABLE, key=str))
async def test_every_monitorable_state_is_actually_swept(
    state: RunState, patched_pipeline: Any
) -> None:
    """Guards the skip logic against being vacuously correct.

    A `MONITORABLE` set that had drifted to exclude everything would make the
    test above pass while monitoring nothing.
    """
    pipeline = patched_pipeline(RecordingPipeline())
    project = await _project(state)

    await _scheduler().tick()

    assert pipeline.calls == [project.id]


# --- the properties that matter unattended --------------------------------


async def test_one_projects_failure_does_not_stop_the_sweep(
    patched_pipeline: Any
) -> None:
    """An unreachable provider must not silently halt monitoring for everyone."""
    pipeline = patched_pipeline(RecordingPipeline(explode=True))
    first = await _project()
    second = await _project()

    result = await _scheduler().tick()

    assert sorted(pipeline.calls) == sorted([first.id, second.id])
    assert set(result.errors) == {str(first.id), str(second.id)}
    assert all(name == "RuntimeError" for name in result.errors.values())
    assert result.checked == []


async def test_a_tick_never_overlaps_itself_for_one_project(
    patched_pipeline: Any
) -> None:
    """Two pipelines on one project would fight over the same run and workspace."""
    pipeline = patched_pipeline(RecordingPipeline(delay=0.15))
    project = await _project()
    scheduler = _scheduler()

    first = asyncio.create_task(scheduler.tick())
    await asyncio.sleep(0.05)
    second = await scheduler.tick()
    await first

    assert pipeline.max_concurrent == 1
    assert second.skipped[str(project.id)] == "a pass is already running"
    assert pipeline.calls == [project.id]


async def test_a_failing_tick_does_not_kill_the_loop(
    patched_pipeline: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler that dies on one bad tick stops monitoring everything."""
    patched_pipeline(RecordingPipeline())
    scheduler = _scheduler()
    calls = {"n": 0}

    async def explode_once(project_id: uuid.UUID) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {}

    monkeypatch.setattr(scheduler, "_pipeline_factory", explode_once)
    await _project()

    await scheduler.tick()  # the failing one, swallowed per project
    result = await scheduler.tick()

    assert calls["n"] == 2
    assert result.checked, "the scheduler kept working after a failure"


# --- lifecycle ------------------------------------------------------------


async def test_starting_and_stopping_is_clean(patched_pipeline: Any) -> None:
    pipeline = patched_pipeline(RecordingPipeline())
    await _project()
    scheduler = _scheduler()

    assert not scheduler.running
    await scheduler.start()
    assert scheduler.running

    for _ in range(100):
        await asyncio.sleep(0.02)
        if scheduler.ticks:
            break

    await scheduler.stop()

    assert not scheduler.running
    assert scheduler.ticks >= 1
    assert pipeline.calls


async def test_stopping_waits_for_the_pass_in_flight(patched_pipeline: Any) -> None:
    """A pass holds a worktree and may have child processes.

    Cancelling without awaiting would leave both for the startup sweep.
    """
    pipeline = patched_pipeline(RecordingPipeline(delay=0.2))
    await _project()
    scheduler = _scheduler()

    await scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()

    assert not scheduler.running
    assert pipeline.concurrent == 0


async def test_starting_twice_is_a_no_op(patched_pipeline: Any) -> None:
    patched_pipeline(RecordingPipeline())
    scheduler = _scheduler()

    await scheduler.start()
    task = scheduler._task
    await scheduler.start()

    assert scheduler._task is task
    await scheduler.stop()


def test_an_interval_below_one_second_is_refused() -> None:
    """A zero interval is a busy loop, not a schedule."""
    with pytest.raises(ValueError, match="at least 1"):
        ProviderScheduler(interval_seconds=0, pipeline_factory=_collaborators)


# --- the polling fallback -------------------------------------------------


async def test_the_poll_runs_after_the_sweep(patched_pipeline: Any) -> None:
    """Merge detection for deployments that cannot receive webhooks."""
    patched_pipeline(RecordingPipeline())
    polled = {"n": 0}

    async def poll() -> Any:
        polled["n"] += 1

    await _project()
    await _scheduler(poll_factory=poll).tick()

    assert polled["n"] == 1


async def test_a_failing_poll_does_not_fail_the_tick(patched_pipeline: Any) -> None:
    patched_pipeline(RecordingPipeline())

    async def poll() -> Any:
        raise RuntimeError("GitHub is unreachable")

    await _project()
    result = await _scheduler(poll_factory=poll).tick()

    assert result.checked
