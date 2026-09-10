"""C1-06 acceptance: the browser can actually reach the API.

Every frontend test stubs `fetch`, so the suite stayed green while real
cross-origin requests were blocked and the dashboard rendered "unreachable"
forever. These tests exercise the CORS layer itself rather than a stub.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.app import create_app
from backend.models.session import create_all
from backend.shared.config import ConfigurationError, Environment, Settings

FRONTEND = "http://localhost:3000"
HOSTILE = "https://evil.example"


@pytest.fixture
def cors_app(tmp_path: Path) -> FastAPI:
    return create_app(
        Settings(
            CONTINUITY_ENV=Environment.TEST,
            DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'cors.db'}",
            FRONTEND_ORIGIN=FRONTEND,
            _env_file=None,
        )
    )


@pytest_asyncio.fixture
async def cors_client(cors_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with cors_app.router.lifespan_context(cors_app):
        await create_all()
        transport = ASGITransport(app=cors_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_the_frontend_origin_is_allowed(cors_client: AsyncClient) -> None:
    response = await cors_client.get("/health", headers={"Origin": FRONTEND})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND


async def test_credentials_are_allowed_so_the_session_cookie_is_sent(
    cors_client: AsyncClient,
) -> None:
    """The session is an HttpOnly cookie; without this the browser drops it."""
    response = await cors_client.get("/health", headers={"Origin": FRONTEND})

    assert response.headers["access-control-allow-credentials"] == "true"


async def test_the_preflight_request_succeeds(cors_client: AsyncClient) -> None:
    """A credentialed cross-origin request preflights before it is sent."""
    response = await cors_client.options(
        "/auth/logout",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND


async def test_another_origin_is_not_allowed(cors_client: AsyncClient) -> None:
    """A wildcard here would let any site read authenticated responses."""
    response = await cors_client.get("/health", headers={"Origin": HOSTILE})

    assert response.headers.get("access-control-allow-origin") != HOSTILE
    assert response.headers.get("access-control-allow-origin") != "*"


def test_production_refuses_to_guess_the_frontend_origin() -> None:
    """Getting CORS wrong in production is either a broken app or an open one."""
    settings = Settings(CONTINUITY_ENV=Environment.PRODUCTION, _env_file=None)

    with pytest.raises(ConfigurationError) as excinfo:
        _ = settings.frontend_origin

    assert excinfo.value.variable == "FRONTEND_ORIGIN"


def test_development_defaults_to_the_next_dev_server() -> None:
    assert Settings(_env_file=None).frontend_origin == FRONTEND
