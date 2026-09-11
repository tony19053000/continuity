"""The thing that makes Continuity autonomous rather than merely automatic.

Every stage could already run; nothing ran them. This is the loop that does —
an in-process asyncio scheduler that wakes on an interval, checks each project's
providers, and drives the pipeline for whatever it finds
(`02_ARCHITECTURE.md` §15).

What it guarantees, and why each matters:

* **No overlapping passes for one project.** A tick that arrives while the
  previous one is still working is skipped, not queued. Two pipelines on one
  project would fight over the same migration runs and the same workspace.
* **One project's failure does not stop the sweep.** An unreachable provider or
  a broken repository must not silently halt monitoring for everyone else.
* **Shutdown is clean and bounded.** The loop is cancellable, and a cancelled
  pass kills its workspace and its child processes rather than orphaning them.
* **It claims nothing it did not do.** Every tick records what it checked and
  what it skipped, and a tick that could not run a project says so.

Development runs this in-process. Production may replace it with SQS, Step
Functions, or AgentCore Runtime without changing `run_pipeline`, which is the
reason the pipeline takes its collaborators as arguments.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.models import Project, RunState
from backend.models.session import session_scope
from backend.observability.logging import get_logger
from backend.orchestration.pipeline import PipelineResult, run_pipeline

logger = get_logger(__name__)

#: States a project must be in for a pass to start. A project mid-migration is
#: already being worked on; starting a second pass would open a competing run.
MONITORABLE: frozenset[RunState] = frozenset(
    {RunState.MONITORING_ACTIVE, RunState.CHANGE_IRRELEVANT, RunState.VERIFIED}
)


@dataclass(slots=True)
class TickResult:
    """One sweep across every project."""

    started_at: datetime
    checked: list[uuid.UUID] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    results: list[PipelineResult] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def pull_requests(self) -> list[int]:
        return [number for result in self.results for number in result.pull_requests]

    def summary(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "checked": len(self.checked),
            "skipped": dict(self.skipped),
            "errors": dict(self.errors),
            "pull_requests": self.pull_requests,
        }


class ProviderScheduler:
    """Runs the pipeline on an interval, one project at a time.

    `pipeline_factory` supplies the collaborators a pass needs — a model
    provider, a workspace manager, a GitHub client. It is a factory rather than
    a set of stored objects so that a long-lived scheduler does not hold a
    GitHub installation token across its expiry.
    """

    def __init__(
        self,
        *,
        interval_seconds: int,
        pipeline_factory: Callable[[], Awaitable[dict[str, Any]]],
        poll_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be at least 1")
        self._interval = interval_seconds
        self._pipeline_factory = pipeline_factory
        self._poll_factory = poll_factory
        self._task: asyncio.Task[None] | None = None
        #: Projects with a pass in flight. The reason a slow project does not
        #: accumulate overlapping pipelines.
        self._in_flight: set[uuid.UUID] = set()
        self.ticks = 0
        self.last_tick: TickResult | None = None

    # --- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="continuity-scheduler")
        logger.info(
            "continuity.scheduler_started", extra={"interval_seconds": self._interval}
        )

    async def stop(self) -> None:
        """Stop cleanly, waiting for the pass in flight to unwind.

        Waiting matters: a pass holds a git worktree and may have child
        processes running, and cancelling without awaiting would leave both
        behind for the startup sweep to find.
        """
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("continuity.scheduler_stopped", extra={"ticks": self.ticks})

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A scheduler that dies on one bad tick stops monitoring
                # everything, which is worse than any single failure it was
                # reacting to.
                logger.exception(
                    "continuity.scheduler_tick_failed",
                    extra={"error": type(exc).__name__},
                    exc_info=exc,
                )
            await asyncio.sleep(self._interval)

    # --- one sweep -------------------------------------------------------

    async def tick(self) -> TickResult:
        """One pass over every project. Safe to call directly, for tests and ops."""
        self.ticks += 1
        result = TickResult(started_at=datetime.now(UTC))

        async with session_scope() as session:
            projects = list(
                (await session.execute(select(Project))).scalars().all()
            )
            candidates = [(p.id, p.state) for p in projects]

        for project_id, state in candidates:
            if state not in MONITORABLE:
                result.skipped[str(project_id)] = f"state is {state.value}"
                continue
            if project_id in self._in_flight:
                result.skipped[str(project_id)] = "a pass is already running"
                continue

            self._in_flight.add(project_id)
            try:
                result.results.append(await self._run_one(project_id))
                result.checked.append(project_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One project's failure is recorded and the sweep continues.
                result.errors[str(project_id)] = type(exc).__name__
                logger.warning(
                    "continuity.scheduler_project_failed",
                    extra={"project_id": str(project_id), "error": type(exc).__name__},
                )
            finally:
                self._in_flight.discard(project_id)

        if self._poll_factory is not None:
            await self._poll()

        self.last_tick = result
        logger.info("continuity.scheduler_tick", extra=result.summary())
        return result

    async def _run_one(self, project_id: uuid.UUID) -> PipelineResult:
        collaborators = await self._pipeline_factory()
        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project is None:  # pragma: no cover - deleted mid-sweep
                return PipelineResult(project_id=project_id, stopped_at="project gone")
            return await run_pipeline(session, project, **collaborators)

    async def _poll(self) -> None:
        """Ask GitHub about open pull requests, for deployments without webhooks."""
        try:
            await self._poll_factory()  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "continuity.scheduler_poll_failed", extra={"error": type(exc).__name__}
            )
