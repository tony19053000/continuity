"""The two gates on starting a pipeline pass, in one place.

A pass may start when the project is in a state that can take one, and when no
other pass is already running for it. Both questions used to be answered inside
the scheduler, which was fine while the scheduler was the only thing that
started a pass.

Two pipelines on one project fight over the same workspace and open competing
migration runs, so the second is refused. The scheduler is no longer the only
caller. `POST /projects/{id}/run` makes the same
`run_pipeline` call, and a person pressing a button twice — or pressing it while
the hourly tick happens to fire — would have raced the scheduler through a guard
it could not see. So the guard moved here, and both callers hold it.

**Scope, stated rather than implied:** this is process-wide, not
deployment-wide. Two API processes, or an API process and a separate worker,
each hold their own set and would not see each other. Making it real across
processes needs a lock in the database, and that is worth doing before
Continuity is deployed as more than one process. Until then the honest claim is
"no overlapping passes within a process", which is what the scheduler always
actually guaranteed too.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from backend.models.enums import RunState
from backend.shared.errors import ContinuityError

#: States a project must be in for a pass to start. A project mid-migration
#: is already being worked on, and a run waiting on an approval owns its
#: workspace and its state; starting a second pass would be a competing
#: claim on both.
MONITORABLE: frozenset[RunState] = frozenset(
    {RunState.MONITORING_ACTIVE, RunState.CHANGE_IRRELEVANT, RunState.VERIFIED}
)


class PassAlreadyRunning(ContinuityError):
    """A pass is in flight for this project, so a second one is refused.

    409 rather than 500: nothing is broken, the caller asked at the wrong
    moment, and trying again later is the right response. Refused rather than
    queued, because a queued pass would run against a repository state its
    caller never saw.
    """

    code = "pass_already_running"
    status_code = 409
    message = "A pass is already running for this project."

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(
            f"A pass is already running for project {project_id}.",
            project_id=str(project_id),
        )


_lock = asyncio.Lock()
_running: set[uuid.UUID] = set()


def running(project_id: uuid.UUID) -> bool:
    """Whether a pass is in flight. Advisory — use `exclusive_pass` to act."""
    return project_id in _running


@asynccontextmanager
async def exclusive_pass(project_id: uuid.UUID) -> AsyncIterator[None]:
    """Hold the project for one pass, or refuse.

    The check and the claim happen under one lock, so two callers arriving at
    the same moment cannot both see an empty set and both proceed. Released in
    a `finally`, so a pass that raises does not leave a project permanently
    unable to run again.
    """
    async with _lock:
        if project_id in _running:
            raise PassAlreadyRunning(project_id)
        _running.add(project_id)

    try:
        yield
    finally:
        async with _lock:
            _running.discard(project_id)
