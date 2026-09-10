"""C1-03 acceptance: `alembic upgrade head` produces the documented schema.

Testing the migration rather than only `create_all()` matters because they are
different code paths. Production schema arrives through Alembic; if the two ever
drift, the tests would pass against a schema nobody actually deploys.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from backend.models import Base
from tests.unit.models.test_tables import EXPECTED_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A database built by running the real migrations."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("DATABASE_URL", url)

    from backend.shared.config import get_settings

    get_settings.cache_clear()

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "head")

    get_settings.cache_clear()
    return f"sqlite:///{tmp_path / 'migrated.db'}"


def test_migration_creates_every_documented_table(migrated_database: str) -> None:
    engine = create_engine(migrated_database)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    missing = EXPECTED_TABLES - names
    assert not missing, f"migration did not create: {sorted(missing)}"


def test_migration_schema_matches_the_models(migrated_database: str) -> None:
    """Every model column must exist in the migrated schema.

    Catches the common failure where a model gains a column and nobody
    regenerates the migration.
    """
    engine = create_engine(migrated_database)
    try:
        inspector = inspect(engine)
        for table_name, table in Base.metadata.tables.items():
            actual = {column["name"] for column in inspector.get_columns(table_name)}
            expected = {column.name for column in table.columns}
            assert expected <= actual, (
                f"{table_name} is missing columns in the migration: {sorted(expected - actual)}"
            )
    finally:
        engine.dispose()


def test_migration_contains_no_hardcoded_connection_string() -> None:
    """No committed file may carry a database URL."""
    ini = (REPO_ROOT / "alembic.ini").read_text()

    for line in ini.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("sqlalchemy.url = "), (
            "alembic.ini must not contain a connection string; env.py reads it from Settings"
        )
