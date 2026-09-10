"""Async engine and session management.

Callers outside `backend/models/` obtain sessions through the FastAPI
dependency in `backend/api/deps.py` or through a repository accessor — agents
never touch a session directly (`02_ARCHITECTURE.md` §3 layering rule).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.models.base import Base
from backend.shared.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def ensure_sqlite_parent(url: str) -> None:
    """Create the directory holding a SQLite database file, if needed.

    Shared by the application and by Alembic so a first run — or a fresh
    checkout where `data/` is gitignored and therefore absent — does not fail
    with an opaque "unable to open database file".
    """
    if url.startswith("sqlite") and ":memory:" not in url:
        Path(url.split("///", 1)[-1]).parent.mkdir(parents=True, exist_ok=True)


def init_engine(settings: Settings) -> AsyncEngine:
    """Create the process-wide engine. Idempotent."""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    url = settings.database_url
    ensure_sqlite_parent(url)

    _engine = create_async_engine(url, echo=False, future=True)

    if url.startswith("sqlite"):
        _enable_sqlite_foreign_keys(_engine)

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Turn on SQLite foreign key enforcement for every connection.

    SQLite disables foreign keys by default. Without this, a development
    database would silently accept the very rows the schema is meant to
    forbid — including an approval pointing at a user that does not exist —
    and the violation would only surface after moving to PostgreSQL. The
    constraint must mean the same thing in both.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine is not initialised; call init_engine() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database engine is not initialised; call init_engine() first.")
    return _session_factory


async def dispose_engine() -> None:
    """Release pooled connections. Called on shutdown and between tests."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transactional session, committed on success and rolled back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """Create every table directly from the metadata.

    For tests and first-run development only. Production schema changes go
    through Alembic so they are reviewable and reversible.
    """
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def check_connection() -> bool:
    """Whether the database is reachable. Backs the `/health` database check."""
    from sqlalchemy import text

    try:
        engine = get_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
