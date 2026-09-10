"""Shared test fixtures.

Every test runs against an isolated on-disk SQLite database in a temporary
directory. In-memory SQLite is avoided because each async connection would get
its own private database, which hides real schema problems.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.app import create_app
from backend.models.session import create_all, dispose_engine, init_engine
from backend.shared.config import Environment, Settings

SAMPLE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample_repo"


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A pristine copy of the sample repository.

    Tests index a copy rather than the committed fixture so that a stray
    artifact — a `__pycache__` left by a tool, an editor swap file — cannot
    change an index snapshot or an exclusion count. It also means a test that
    accidentally writes cannot damage the fixture for every other test.
    """
    destination = tmp_path / "sample_repo"
    shutil.copytree(SAMPLE_REPO, destination, ignore=shutil.ignore_patterns("__pycache__"))
    return destination


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at a throwaway database, with no integration configured.

    Starting from "nothing configured" is deliberate: it is the state a fresh
    checkout is in, and it is where honesty about `NotConfigured` matters most.
    """
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        _env_file=None,  # never read a developer's real .env during tests
    )


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[None]:
    init_engine(settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP client with the application lifespan actually running.

    `ASGITransport` does not run lifespan on its own, so the engine would never
    be initialised and every database check would report a false failure.
    """
    async with app.router.lifespan_context(app):
        await create_all()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
