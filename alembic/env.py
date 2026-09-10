"""Alembic environment.

The database URL comes from `Settings`, not from `alembic.ini`, so migrations
run against exactly the database the application uses and no connection string
is ever written into a committed file.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.models import Base

# Importing the table module registers every model on Base.metadata, which is
# what autogenerate compares against.
import backend.models.tables  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    from backend.models.session import ensure_sqlite_parent
    from backend.shared.config import get_settings

    url = get_settings().database_url
    ensure_sqlite_parent(url)
    return url


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render `StrEnumType` columns as plain `sa.String` in migration scripts.

    `StrEnumType` is a Python-side convenience: it stores text and converts back
    to the enum on load. The database only ever sees a VARCHAR, so that is what
    the migration should say. Rendering the decorator instead would force every
    migration to import application code — coupling schema history to code that
    will keep changing, and breaking old migrations when an enum is renamed.
    """
    from backend.models.base import StrEnumType

    if type_ == "type" and isinstance(obj, StrEnumType):
        return f"sa.String(length={obj.impl_instance.length})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead, so the same migration script works on both backends.
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
