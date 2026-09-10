"""Health endpoint.

Reports exactly the four components named in ticket C1-01 and nothing else. In
particular it never reveals a configuration *value* — only whether Continuity
has been told enough to use a given integration.

The `bedrock` and `github_app` checks are deliberately configuration-presence
only. Making a live model call or a GitHub API call from a health probe would
cost money, add latency, and turn an unrelated outage into a failing liveness
check.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.models.session import check_connection
from backend.shared.config import Settings

router = APIRouter(tags=["health"])

type OkState = Literal["ok", "error"]
type ConfiguredState = Literal["configured", "not_configured"]
type QueueState = Literal["ok", "error", "not_configured"]


class HealthComponents(BaseModel):
    database: OkState
    bedrock: ConfiguredState
    github_app: ConfiguredState
    # Three-valued rather than two: no job runner exists until C2-05, and
    # reporting "ok" for a component that does not exist would be exactly the
    # fake-green status the project forbids.
    job_queue: QueueState


class HealthResponse(BaseModel):
    version: str
    status: Literal["ok", "degraded", "error"]
    components: HealthComponents


def _roll_up(components: HealthComponents) -> Literal["ok", "degraded", "error"]:
    """Overall status from component states.

    A component in `error` means `error` — something that should work does not.
    A component that is merely `not_configured` means `degraded`: the service
    runs, but not every feature is available. `ok` requires everything present
    and working.
    """
    if components.database == "error" or components.job_queue == "error":
        return "error"
    if "not_configured" in (
        components.bedrock,
        components.github_app,
        components.job_queue,
    ):
        return "degraded"
    return "ok"


def _job_queue_state() -> QueueState:
    """Real job runner state.

    Returns `not_configured` until a runner actually exists (C2-05). Reporting
    `ok` for a component that has not been built would put a green check in the
    UI backed by nothing — and this page tells the user that nothing on it is
    simulated.
    """
    return "not_configured"


async def build_health(settings: Settings, version: str) -> HealthResponse:
    components = HealthComponents(
        database="ok" if await check_connection() else "error",
        bedrock="configured" if settings.bedrock else "not_configured",
        github_app="configured" if settings.github_app else "not_configured",
        job_queue=_job_queue_state(),
    )
    return HealthResponse(version=version, status=_roll_up(components), components=components)


@router.get("/health", response_model=HealthResponse)
async def get_health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    version: str = request.app.state.version
    return await build_health(settings, version)
