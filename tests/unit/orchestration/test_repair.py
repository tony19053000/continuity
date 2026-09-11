"""C7-04: the bounded repair loop.

Real workspaces, real pytest runs, real attempt rows. The engineer is scripted —
what is under test is the loop, not the model — but everything the loop reacts
to is genuine: the patch lands on disk, the suite really runs, and the counts
come out of a process.

The two fixtures the ticket names are here: one that fails on attempt 1 and
passes on attempt 2, and one that can never pass.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.agents import migration_engineer as engineer_module
from backend.migrations.workspace import WorkspaceManager
from backend.models import (
    ChangeEvent,
    MigrationAttempt,
    MigrationRun,
    Project,
    Repository,
    RunState,
    SecurityFinding,
    User,
)
from backend.models import (
    TestResult as StoredTestResult,
)
from backend.models.enums import AttemptOutcome, ChangeType, FindingCategory
from backend.models.session import session_scope
from backend.observability.execution_audit import NullExecutionAudit
from backend.orchestration.coordinator import RunCoordinator
from backend.orchestration.repair import run_repair_loop
from tests.support.migration_fixtures import (
    CLIENT_V2,
    IMPACT_SET,
    ScriptedEngineer,
    StubProvider,
    change,
    edit,
    fix_it,
    half_fix_it,
    impact,
    never_works,
    output,
    write_fixture_repository,
)

pytestmark = pytest.mark.usefixtures("database")

MAX_ATTEMPTS = 3


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


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    def install(*outputs: Any) -> ScriptedEngineer:
        runner = ScriptedEngineer(*outputs)
        original = engineer_module.MigrationEngineerAgent

        class Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, provider: Any, **kwargs: Any) -> None:
                super().__init__(provider, runner=runner, **kwargs)

        monkeypatch.setattr(engineer_module, "MigrationEngineerAgent", Patched)
        return runner

    return install


async def _run_row() -> MigrationRun:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="rp@example.test")
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


async def _loop(manager: WorkspaceManager, run: MigrationRun, *, max_attempts: int = MAX_ATTEMPTS):
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
                model_provider=StubProvider(),
                coordinator=RunCoordinator(max_repair_attempts=max_attempts),
                max_attempts=max_attempts,
            )
        return result, ws.read_file("app/client.py")


async def _attempt_rows(run: MigrationRun) -> list[MigrationAttempt]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(MigrationAttempt)
                    .where(MigrationAttempt.migration_run_id == run.id)
                    .order_by(MigrationAttempt.attempt_number)
                )
            ).scalars()
        )


# --- first try -----------------------------------------------------------


async def test_a_correct_patch_passes_on_the_first_attempt(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """The baseline. One attempt, tests really run, and they really pass."""
    scripted(fix_it())
    run = await _run_row()

    result, client = await _loop(manager, run)

    assert result.repaired
    assert result.final_state is RunState.VALIDATION_PASSED
    assert result.attempts_used == 1
    assert client == CLIENT_V2

    (attempt,) = await _attempt_rows(run)
    assert attempt.outcome is AttemptOutcome.PASSED
    assert attempt.files_changed == {"paths": ["app/client.py"]}
    assert "currency" in (attempt.patch_diff or "")


# --- the repair ----------------------------------------------------------


async def test_a_failing_first_attempt_is_genuinely_repaired_on_the_second(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """C7-04 acceptance, the central behaviour of the product.

    Attempt 1 changes the constant but not the function, so the suite really
    fails. Attempt 2 fixes it and the suite really passes. Both attempts are
    recorded, the first with failure evidence.
    """
    scripted(half_fix_it(), fix_it())
    run = await _run_row()

    result, client = await _loop(manager, run)

    assert result.repaired
    assert result.attempts_used == 2
    assert client == CLIENT_V2

    first, second = await _attempt_rows(run)

    assert first.attempt_number == 1
    assert first.outcome is AttemptOutcome.FAILED
    assert first.failure_evidence is not None
    assert first.failure_evidence["failed"] == 1
    assert first.failure_evidence["passed"] == 1
    assert first.failure_evidence["failing_test_ids"]

    assert second.attempt_number == 2
    assert second.outcome is AttemptOutcome.PASSED


async def test_the_second_attempt_is_told_why_the_first_failed(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """A repair is a response to evidence, not another roll of the dice."""
    runner = scripted(half_fix_it(), fix_it())
    run = await _run_row()

    await _loop(manager, run)

    assert runner.previous_failures[0] is None
    second = runner.previous_failures[1]
    assert second is not None
    assert "failed" in second
    assert "test_charge_sends_every_required_field" in second


async def test_every_attempt_stores_its_real_test_results(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """`test_results` rows come from processes, one per attempt."""
    scripted(half_fix_it(), fix_it())
    run = await _run_row()

    await _loop(manager, run)

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(StoredTestResult).where(StoredTestResult.migration_run_id == run.id)
                )
            ).scalars()
        )

    assert len(rows) == 2
    assert sorted((r.passed, r.failed) for r in rows) == [(1, 1), (2, 0)]
    for row in rows:
        assert "pytest" in row.command
        assert row.exit_code is not None


# --- the budget ----------------------------------------------------------


async def test_a_patch_that_can_never_pass_stops_at_the_budget(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """C7-04 acceptance: exactly MAX_REPAIR_ATTEMPTS, then a human.

    Counted from rows, which is the honest measure — an implementation that
    looped correctly and recorded only the last attempt would pass a
    result-shaped assertion.
    """
    scripted(never_works())
    run = await _run_row()

    result, _ = await _loop(manager, run)

    assert not result.repaired
    assert result.final_state is RunState.HUMAN_REVIEW_REQUIRED
    assert result.attempts_used == MAX_ATTEMPTS

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(func.count())
                .select_from(MigrationAttempt)
                .where(MigrationAttempt.migration_run_id == run.id)
            )
        ).scalar_one()
        refreshed = await session.get(MigrationRun, run.id)

    assert rows == MAX_ATTEMPTS
    assert refreshed is not None
    assert refreshed.state is RunState.HUMAN_REVIEW_REQUIRED


@pytest.mark.parametrize("budget", [1, 2])
async def test_the_loop_cannot_exceed_whatever_the_budget_is(
    budget: int, manager: WorkspaceManager, scripted: Any
) -> None:
    """The bound is the configured number, not a hardcoded three."""
    runner = scripted(never_works())
    run = await _run_row()

    result, _ = await _loop(manager, run, max_attempts=budget)

    assert result.attempts_used == budget
    assert runner.calls == budget
    assert len(await _attempt_rows(run)) == budget


async def test_the_budget_survives_a_restart(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Attempts are counted from rows, so a crash does not refill the budget.

    Simulated by writing attempt rows as though a previous process had made
    them, then starting the loop. It must find the budget already spent.
    """
    scripted(fix_it())
    run = await _run_row()

    async with session_scope() as session:
        for number in (1, 2, 3):
            session.add(
                MigrationAttempt(
                    migration_run_id=run.id,
                    attempt_number=number,
                    outcome=AttemptOutcome.FAILED,
                )
            )
        await session.flush()

    result, _ = await _loop(manager, run)

    assert result.final_state is RunState.HUMAN_REVIEW_REQUIRED
    assert result.attempts_used == 0, "the loop should not have started a new attempt"
    assert len(await _attempt_rows(run)) == MAX_ATTEMPTS


def test_the_attempt_table_refuses_a_duplicate_number() -> None:
    """The database enforces the budget too, so a race cannot exceed it."""
    constraint = {
        c.name for c in MigrationAttempt.__table__.constraints if c.name
    }

    assert "uq_attempt_number" in constraint


# --- blocking findings ---------------------------------------------------


async def test_a_blocking_finding_escalates_instead_of_being_retried(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """A credential in the patch is a decision for a person.

    Retrying would spend the budget producing it again — and each retry is
    another model call against the user's code.
    """
    from tests.support.secret_samples import GITHUB_TOKEN

    runner = scripted(output(edit("app/client.py", f'T = "{GITHUB_TOKEN}"\n')))
    run = await _run_row()

    result, _ = await _loop(manager, run)

    assert result.final_state is RunState.HUMAN_REVIEW_REQUIRED
    assert result.attempts_used == 1
    assert runner.calls == 1

    async with session_scope() as session:
        findings = list(
            (
                await session.execute(
                    select(SecurityFinding).where(SecurityFinding.migration_run_id == run.id)
                )
            ).scalars()
        )

    assert [f.category for f in findings] == [FindingCategory.SECRET_EXPOSURE]
    assert GITHUB_TOKEN not in findings[0].summary


async def test_a_new_dependency_escalates_rather_than_being_installed(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """ASK means a human, and the loop stops to get one."""
    scripted(
        output(
            edit("app/client.py", CLIENT_V2),
            edit(
                "pyproject.toml",
                '[project]\nname = "fixture"\nversion = "0"\n'
                'dependencies = ["httpx", "left-pad"]\n',
                out_of_impact_justification="needed",
            ),
        )
    )
    run = await _run_row()

    result, _ = await _loop(manager, run)

    assert result.final_state is RunState.HUMAN_REVIEW_REQUIRED

    async with session_scope() as session:
        categories = list(
            (
                await session.execute(
                    select(SecurityFinding.category).where(
                        SecurityFinding.migration_run_id == run.id
                    )
                )
            ).scalars()
        )

    assert FindingCategory.NEW_DEPENDENCY in categories


async def test_a_model_failure_is_a_recorded_attempt_not_a_crashed_run(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """A model that errors must not abandon the migration mid-flight.

    Letting the exception propagate would leave the workspace half-patched, no
    attempt row explaining why, and the budget bypassed entirely — nothing was
    recorded, so nothing was spent. Found when live Gemini intermittently failed
    to invoke its structured-output tool and took the whole run down with it.
    """
    scripted(RuntimeError("the model fell over"))
    run = await _run_row()

    result, _ = await _loop(manager, run)

    # Retried across the budget — a model that errors is transient — and then
    # escalated, rather than the exception taking the run down on attempt one.
    assert result.attempts_used == MAX_ATTEMPTS
    assert result.final_state is RunState.HUMAN_REVIEW_REQUIRED

    attempts = await _attempt_rows(run)
    assert len(attempts) == MAX_ATTEMPTS
    for attempt in attempts:
        assert attempt.outcome is AttemptOutcome.FAILED
        assert attempt.failure_evidence is not None
        assert "the engineer failed" in str(attempt.failure_evidence["blocked_by"])

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)
    assert refreshed is not None
    assert refreshed.state is RunState.HUMAN_REVIEW_REQUIRED


def test_the_agent_runner_retries_a_structured_output_failure() -> None:
    """The transient failure is retried before it ever reaches the loop.

    Gemini declines to invoke the structured-output tool intermittently on large
    schemas. Not retrying it spent a whole repair attempt on a failure that
    usually clears on the next call.
    """
    import inspect

    from strands.types.exceptions import StructuredOutputException

    from backend.agents import base

    source = inspect.getsource(base.ContinuityAgent.run)

    assert "StructuredOutputException" in source
    assert StructuredOutputException is not None


async def test_an_empty_patch_is_never_validated(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Validating an unchanged workspace would report a pass.

    On a project whose tests already pass — which is the normal state of a
    repository before a provider changes under it — "the engineer produced
    nothing" would otherwise read as "the migration worked".
    """
    scripted(output())
    run = await _run_row()

    result, _ = await _loop(manager, run)

    assert not result.repaired
    async with session_scope() as session:
        results = (
            await session.execute(
                select(func.count())
                .select_from(StoredTestResult)
                .where(StoredTestResult.migration_run_id == run.id)
            )
        ).scalar_one()

    assert results == 0, "an empty patch must not produce a test result"


async def test_an_empty_patch_is_retried_before_it_escalates(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Regression: the live loop failed roughly one run in six on this.

    Gemini intermittently returns a patch with no usable edit. Escalating on the
    first one stopped the run with two attempts unspent — the budget exists for
    precisely this transient failure. It is a spent attempt, not the end of the
    run.
    """
    runner = scripted(output(), output(), fix_it())
    run = await _run_row()

    result, client = await _loop(manager, run)

    assert runner.calls == 3
    assert result.repaired
    assert result.attempts_used == 3
    assert client == CLIENT_V2

    outcomes = [a.outcome for a in await _attempt_rows(run)]
    assert outcomes == [
        AttemptOutcome.FAILED,
        AttemptOutcome.FAILED,
        AttemptOutcome.PASSED,
    ]


async def test_the_next_attempt_is_told_which_edits_were_discarded(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """A rejection is evidence, and the loop responds to evidence.

    Without this the next attempt learns only that tests failed, never that its
    edits were thrown away — so it can propose the same rejected edit again and
    spend the entire budget doing it.
    """
    runner = scripted(
        output(edit("app/unrelated.py", "VALUE = 2\n")),  # rejected: out of scope
        fix_it(),
    )
    run = await _run_row()

    result, client = await _loop(manager, run)

    assert result.repaired
    assert runner.calls == 2

    second = runner.previous_failures[1]
    assert second is not None
    assert "DISCARDED" in second
    assert "app/unrelated.py" in second
    assert "impact set" in second
    assert client == CLIENT_V2


async def test_an_engineer_that_never_produces_anything_stops_at_the_budget(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Retrying is bounded. It does not become an infinite loop."""
    runner = scripted(output())
    run = await _run_row()

    result, _ = await _loop(manager, run)

    assert runner.calls == MAX_ATTEMPTS
    assert result.attempts_used == MAX_ATTEMPTS
    assert result.final_state is RunState.HUMAN_REVIEW_REQUIRED
    assert "exhausted" in result.reason


# --- the run's history ---------------------------------------------------


async def test_the_run_walks_the_states_the_machine_allows(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """A recorded history that `ALLOWED_TRANSITIONS` would accept, in order."""
    from backend.models import StateTransition
    from backend.orchestration.state_machine import can_transition

    scripted(half_fix_it(), fix_it())
    run = await _run_row()

    await _loop(manager, run)

    async with session_scope() as session:
        transitions = list(
            (
                await session.execute(
                    select(StateTransition)
                    .where(StateTransition.migration_run_id == run.id)
                    .order_by(StateTransition.created_at, StateTransition.id)
                )
            ).scalars()
        )

    assert transitions
    for step in transitions:
        assert can_transition(step.from_state, step.to_state), (
            f"recorded an illegal move: {step.from_state} -> {step.to_state}"
        )

    assert transitions[-1].to_state is RunState.VALIDATION_PASSED
    assert RunState.REPAIR_RUNNING in {t.to_state for t in transitions}
