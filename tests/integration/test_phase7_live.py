"""Phase 7 against the live model.

The scripted-engineer tests prove the rules around the patch. This proves the
agent can actually write one: a real Gemini call, given a real failing project,
producing an edit that makes a real pytest run go green.

Skips with a named blocker when Gemini is unconfigured; never passes without it.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.agents.migration_engineer import produce_patch
from backend.migrations.workspace import WorkspaceManager
from backend.models import (
    ChangeEvent,
    MigrationAttempt,
    MigrationRun,
    Project,
    Repository,
    RunState,
    User,
)
from backend.models.enums import ChangeType
from backend.models.session import session_scope
from backend.observability.execution_audit import NullExecutionAudit
from backend.orchestration.coordinator import RunCoordinator
from backend.orchestration.repair import run_repair_loop
from backend.shared.config import GeminiConfig, Settings
from backend.shared.model_provider import build_model_provider
from backend.validation.runner import run_tests
from tests.support.migration_fixtures import (
    IMPACT_SET,
    change,
    impact,
    write_fixture_repository,
)

pytestmark = pytest.mark.requires_gemini

MAX_ATTEMPTS = 3


def _provider_or_skip():
    settings = Settings()
    gemini = settings.gemini
    if not isinstance(gemini, GeminiConfig):
        pytest.skip(
            "SKIPPED, NOT PASSED — Google Gemini is not configured: "
            f"{gemini.reason}. Patch generation is unproven until this runs."
        )
    return build_model_provider(settings)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "userrepo"
    repo.mkdir()
    write_fixture_repository(repo)

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607 - fixture setup
            cwd=repo,
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )

    run("init", "-b", "main")
    run("config", "user.email", "t@example.test")
    run("config", "user.name", "Test")
    run("add", "-A")
    run("commit", "-m", "initial")
    return repo


@pytest.fixture
def manager(source_repo: Path, tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(
        source_repo, workspace_root=tmp_path / "ws", audit=NullExecutionAudit()
    )


async def _run_row() -> MigrationRun:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="l7@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(user_id=user.id, repository_id=repository.id, name="p")
        session.add(project)
        await session.flush()
        event = ChangeEvent(
            provider_id="acmepay",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.REQUEST_FIELD_REQUIRED,
            resource=f"POST /v1/charges request.currency {uuid.uuid4().hex[:6]}",
            breaking=True,
            source={"kind": "openapi_spec"},
            evidence={"kind": "provider_spec", "confidence": "confirmed"},
            detected_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()
        run = MigrationRun(
            project_id=project.id,
            change_event_id=event.id,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            state=RunState.MIGRATION_PENDING,
        )
        session.add(run)
        await session.flush()
        await session.refresh(run)
        return run


async def test_the_live_engineer_writes_a_patch_that_lands(
    manager: WorkspaceManager,
) -> None:
    """C7-02 with a real model.

    Note what the fixture is: a project that is **green** against v1 and whose
    own tests encode the old assumption. That is the realistic shape — the
    provider changed, the code is now wrong, and the suite does not know it yet.
    Proving the incompatibility is the rehearsal's job (C6-04), not the test
    suite's.

    So the assertion is that a real model produces a well-formed, in-scope patch
    that adapts the client to the new contract. Whether it gets it *right* first
    time is not asserted here: a plausible mistake — making `currency` a
    required positional argument, which breaks `charge(100)` — is exactly what
    the repair loop exists to catch, and the loop test below is where that is
    proven end to end.
    """
    provider = _provider_or_skip()

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        before = await run_tests(ws, suite="before")
        assert before.ok, "the fixture should start green against v1"

        patch = await produce_patch(
            ws,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            change=change(),
            impact=impact(),
            impact_set=IMPACT_SET,
            model_provider=provider,
        )

        after = await run_tests(ws, suite="after")

    assert patch.applied == ["app/client.py"], (
        f"expected only the impact-set file to change; rejected={patch.rejected}"
    )
    assert patch.blocking_findings == []
    # The migration actually happened, rather than the model changing nothing.
    assert "currency" in patch.diff
    # The suite still runs and reports counts — a patch that left the file
    # unimportable would produce none.
    assert after.total == 2


async def test_the_live_engineer_stays_inside_the_impact_set(
    manager: WorkspaceManager,
) -> None:
    """The scope rule holds against a real model, not just a scripted one."""
    provider = _provider_or_skip()

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        patch = await produce_patch(
            ws,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            change=change(),
            impact=impact(),
            impact_set=IMPACT_SET,
            model_provider=provider,
        )
        changed = await ws.changed_files()
        unrelated = ws.read_file("app/unrelated.py")
        suite = ws.read_file("tests/test_client.py")

    # Not vacuous: an empty patch would satisfy "changed nothing outside the
    # set" while proving nothing. It has to have actually edited something.
    assert patch.applied == ["app/client.py"]
    assert patch.rejected == [], f"edits were discarded: {patch.rejected}"
    assert changed == ["app/client.py"]
    assert unrelated == "VALUE = 1\n"
    assert "def test_charge_sends_every_required_field" in suite, (
        "the live engineer weakened or removed a test"
    )


@pytest.mark.usefixtures("database")
async def test_the_live_repair_loop_completes_a_migration(
    manager: WorkspaceManager,
) -> None:
    """C7-04 end to end with a live model.

    Everything real: the patch comes from Gemini, the tests run in a process,
    and the attempt rows record what happened.
    """
    provider = _provider_or_skip()
    run = await _run_row()

    async with manager.open(run_id=run.id, target_branch="continuity/x") as ws:
        async with session_scope() as session:
            tracked = await session.get(MigrationRun, run.id)
            assert tracked is not None
            result = await run_repair_loop(
                session,
                tracked,
                ws,
                change=change(),
            impact=impact(),
                impact_set=IMPACT_SET,
                model_provider=provider,
                coordinator=RunCoordinator(max_repair_attempts=MAX_ATTEMPTS),
                max_attempts=MAX_ATTEMPTS,
            )

    async with session_scope() as session:
        attempts = list(
            (
                await session.execute(
                    select(MigrationAttempt)
                    .where(MigrationAttempt.migration_run_id == run.id)
                    .order_by(MigrationAttempt.attempt_number)
                )
            ).scalars()
        )

    # The budget holds whatever the model does.
    assert 1 <= len(attempts) <= MAX_ATTEMPTS
    assert result.attempts_used == len(attempts)

    assert result.repaired, (
        f"the live loop did not repair the fixture in {MAX_ATTEMPTS} attempts: "
        f"{result.reason}"
    )
    assert attempts[-1].patch_diff
    assert "currency" in (attempts[-1].patch_diff or "")
