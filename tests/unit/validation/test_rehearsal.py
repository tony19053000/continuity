"""C6-04: proving the incompatibility before touching user code.

These tests really run pytest, in a real workspace, through the real
`ExecutionProvider`. The pass counts asserted below came out of a process. That
is the point of the ticket — a mocked executor would prove only that the mock
was configured as expected, and "no result is ever model-generated" would be
untested.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.models import (
    ChangeEvent,
    MigrationRun,
    Project,
    Repository,
    RunState,
    User,
)
from backend.models import (
    TestResult as StoredTestResult,
)
from backend.models.enums import (
    ChangeType,
    Confidence,
    EvidenceKind,
    SourceKind,
)
from backend.models.schemas import Evidence, ProviderChange, SourceRef
from backend.models.session import session_scope
from backend.observability.execution_audit import NullExecutionAudit
from backend.providers.base import ExternalDocument
from backend.providers.storage import store_document
from backend.shared.execution import DevelopmentIsolatedExecutor
from backend.validation.rehearsal import (
    ContractSide,
    NoSimulationAdapter,
    RehearsalResult,
    parse_pytest_output,
    proceed_after_rehearsal,
    rehearse,
    selected_test_ids,
)
from tests.support.rehearsal_fixtures import (
    BrokenAdapter,
    ContractFileAdapter,
    write_fixture_repository,
)

pytestmark = pytest.mark.usefixtures("database")

SOURCE = SourceRef(kind=SourceKind.OPENAPI_SPEC, url="https://acmepay.test/spec")


def _change() -> ProviderChange:
    return ProviderChange(
        provider_id="acmepay",
        old_version="v1",
        new_version="v2",
        change_type=ChangeType.REQUEST_FIELD_REQUIRED,
        resource="POST /v1/charges request.currency",
        breaking=True,
        security_relevant=False,
        authentication_relevant=False,
        source=SOURCE,
        evidence=Evidence(
            kind=EvidenceKind.PROVIDER_SPEC,
            confidence=Confidence.CONFIRMED,
            source_ref=SOURCE,
        ),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def executor(workspace: Path) -> DevelopmentIsolatedExecutor:
    return DevelopmentIsolatedExecutor(workspace, audit=NullExecutionAudit())


async def _run(*, with_specs: bool = True) -> MigrationRun:
    """A migration run in REHEARSAL_PENDING, with specs stored for both sides."""
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="r@example.test")
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
            source=SOURCE.model_dump(mode="json"),
            evidence={"kind": "provider_spec", "confidence": "confirmed"},
            detected_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()

        if with_specs:
            for version in ("v1", "v2"):
                await store_document(
                    session,
                    "acmepay",
                    version,
                    ExternalDocument(
                        kind=SourceKind.OPENAPI_SPEC,
                        content=json.dumps({"openapi": "3.1.0", "info": {"version": version}}),
                        url=f"https://acmepay.test/{version}.json",
                        version=version,
                    ),
                )

        run = MigrationRun(
            project_id=project.id,
            change_event_id=event.id,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            state=RunState.REHEARSAL_PENDING,
        )
        session.add(run)
        await session.flush()
        await session.refresh(run)
        return run


# --- the confirmed path --------------------------------------------------


async def test_a_real_difference_is_reproduced_and_confirmed(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """C6-04 acceptance: old-versus-new pass counts from real execution.

    The fixture suite has two tests. One depends on the field that became
    required, one does not. Against the old contract both pass; against the new
    one exactly one fails. Those numbers came out of pytest.
    """
    selector = write_fixture_repository(workspace)
    run = await _run()
    adapter = ContractFileAdapter()

    async with session_scope() as session:
        result = await rehearse(
            session,
            run,
            _change(),
            executor=executor,
            adapter=adapter,
            workspace=workspace,
            selected_tests=[selector],
            affected_workflows=["Checkout"],
        )

    assert result.outcome is RunState.REHEARSAL_CONFIRMED
    assert result.reproduced_a_difference
    assert adapter.prepared == [ContractSide.OLD, ContractSide.NEW]

    assert result.old is not None and result.new is not None
    assert (result.old.passed, result.old.failed) == (2, 0)
    assert (result.new.passed, result.new.failed) == (1, 1)
    assert result.newly_failing == ["test_payments.py::test_charge_is_accepted"]
    assert result.affected_workflows == ["Checkout"]


async def test_the_delta_is_stored_as_evidence_on_the_run(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    selector = write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        await rehearse(
            session, run, _change(),
            executor=executor, adapter=ContractFileAdapter(),
            workspace=workspace, selected_tests=[selector],
            affected_workflows=["Checkout"],
        )

    async with session_scope() as session:
        stored = await session.get(MigrationRun, run.id)

    assert stored is not None
    assert stored.state is RunState.REHEARSAL_CONFIRMED
    rehearsal = stored.rehearsal
    assert rehearsal is not None
    assert rehearsal["outcome"] == "rehearsal_confirmed"
    assert rehearsal["old"]["passed"] == 2
    assert rehearsal["new"]["failed"] == 1
    assert rehearsal["affected_workflows"] == ["Checkout"]


async def test_both_sides_are_stored_as_test_results(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """`test_results` may hold nothing a model produced.

    Two rows, one per side, each carrying the argv that produced it — so the
    numbers can be traced back to a command that really ran.
    """
    selector = write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        await rehearse(
            session, run, _change(),
            executor=executor, adapter=ContractFileAdapter(),
            workspace=workspace, selected_tests=[selector],
        )

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(StoredTestResult).where(StoredTestResult.migration_run_id == run.id)
            )
        ).scalars().all()

    assert sorted(r.suite for r in rows) == ["rehearsal:new", "rehearsal:old"]
    for row in rows:
        assert "pytest" in row.command
        assert row.exit_code is not None
        assert row.raw_output_excerpt


async def test_a_confirmed_rehearsal_proceeds_to_migration(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    selector = write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor, adapter=ContractFileAdapter(),
            workspace=workspace, selected_tests=[selector],
        )
        state = await proceed_after_rehearsal(session, run, result)

    assert state is RunState.MIGRATION_PENDING


# --- no difference -------------------------------------------------------


async def test_a_rehearsal_reproducing_no_difference_escalates(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """C6-04 acceptance. The provider said something changed; the code disagrees.

    Migrating anyway would mean patching working code on a provider's say-so,
    which is the failure mode rehearsal exists to prevent.
    """
    selector = write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor,
            adapter=ContractFileAdapter(difference=False),
            workspace=workspace,
            selected_tests=[selector],
        )

    assert result.outcome is RunState.REHEARSAL_FAILED
    assert result.newly_failing == []
    assert result.old is not None and result.new is not None
    assert (result.old.passed, result.new.passed) == (2, 2)
    assert "reproduced no difference" in result.reason


async def test_a_failed_rehearsal_escalates_instead_of_migrating(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    selector = write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor,
            adapter=ContractFileAdapter(difference=False),
            workspace=workspace,
            selected_tests=[selector],
        )
        state = await proceed_after_rehearsal(session, run, result)

    assert state is RunState.HUMAN_REVIEW_REQUIRED


def test_the_state_machine_forbids_migrating_from_a_failed_rehearsal() -> None:
    """Belt and braces: the rule is in `ALLOWED_TRANSITIONS`, not only in code.

    `proceed_after_rehearsal` chooses among legal moves. What makes migrating on
    an unreproduced difference impossible is that the edge does not exist.
    """
    from backend.orchestration.state_machine import ALLOWED_TRANSITIONS

    assert RunState.MIGRATION_PENDING not in ALLOWED_TRANSITIONS[RunState.REHEARSAL_FAILED]
    assert RunState.MIGRATION_PENDING in ALLOWED_TRANSITIONS[RunState.REHEARSAL_CONFIRMED]
    assert RunState.MIGRATION_PENDING in ALLOWED_TRANSITIONS[RunState.REHEARSAL_UNAVAILABLE]


# --- unavailable ---------------------------------------------------------


async def test_no_simulation_adapter_makes_the_rehearsal_unavailable(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """The honest default.

    Running the same suite twice and reporting the inevitable non-difference
    would be a fabricated result — and it would read as REHEARSAL_FAILED, which
    says "we checked", not "we could not check".
    """
    selector = write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor, adapter=NoSimulationAdapter(),
            workspace=workspace, selected_tests=[selector],
        )

    assert result.outcome is RunState.REHEARSAL_UNAVAILABLE
    assert "no provider simulation adapter" in result.reason
    assert result.old is None and result.new is None


async def test_a_missing_spec_makes_the_rehearsal_unavailable(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """C6-04 acceptance, named exactly: no usable spec.

    `latest_spec` returns only parseable documents, so this covers both "never
    fetched" and "fetched but unreadable" — in either case there is no contract
    to rehearse against.
    """
    selector = write_fixture_repository(workspace)
    run = await _run(with_specs=False)

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor, adapter=ContractFileAdapter(),
            workspace=workspace, selected_tests=[selector],
        )

    assert result.outcome is RunState.REHEARSAL_UNAVAILABLE
    assert "no usable acmepay specification" in result.reason


async def test_no_covering_test_makes_the_rehearsal_unavailable(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """Nothing to run is not the same as nothing to find."""
    write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor, adapter=ContractFileAdapter(),
            workspace=workspace, selected_tests=[],
        )

    assert result.outcome is RunState.REHEARSAL_UNAVAILABLE
    assert "no test in this repository covers" in result.reason


async def test_an_unavailable_rehearsal_stores_its_reason_and_still_proceeds(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """C6-04 acceptance: unavailable proceeds to MIGRATION_PENDING.

    "We could not check" is not "nothing is wrong". The reason is stored so the
    pull request can say the incompatibility was never reproduced, rather than
    implying it was.
    """
    selector = write_fixture_repository(workspace)
    run = await _run(with_specs=False)

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor, adapter=ContractFileAdapter(),
            workspace=workspace, selected_tests=[selector],
        )
        state = await proceed_after_rehearsal(session, run, result)

    async with session_scope() as session:
        stored = await session.get(MigrationRun, run.id)

    assert state is RunState.MIGRATION_PENDING
    assert stored is not None
    assert stored.rehearsal is not None
    assert stored.rehearsal["reason"]
    assert stored.rehearsal["outcome"] == "rehearsal_unavailable"


async def test_a_suite_that_cannot_run_is_unavailable_not_a_pass(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """Zero counts must never read as zero failures.

    A suite that fails to collect produces no numbers at all. Treating that as
    "0 failed on both sides" would report REHEARSAL_FAILED — "we checked and
    found nothing" — for a check that never happened.
    """
    selector = write_fixture_repository(workspace)
    run = await _run()

    async with session_scope() as session:
        result = await rehearse(
            session, run, _change(),
            executor=executor, adapter=BrokenAdapter(),
            workspace=workspace, selected_tests=[selector],
        )

    assert result.outcome is RunState.REHEARSAL_UNAVAILABLE
    assert "no test counts" in result.reason


# --- parsing -------------------------------------------------------------


def _result(stdout: str, *, exit_code: int = 1, timed_out: bool = False):
    from backend.shared.execution import CommandResult, ExecutionStatus

    return CommandResult(
        argv=["python", "-m", "pytest"],
        cwd=Path(__file__).parent,
        status=ExecutionStatus.TIMED_OUT if timed_out else ExecutionStatus.COMPLETED,
        exit_code=None if timed_out else exit_code,
        duration_ms=10,
        stdout=stdout,
        stderr="",
    )


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("= 11 passed in 0.42s =", (11, 0, 0)),
        ("= 3 failed, 11 passed in 0.42s =", (11, 3, 0)),
        ("= 3 failed, 11 passed, 2 skipped in 0.4s =", (11, 3, 2)),
        # Errors are failures. A suite that could not import has not passed.
        ("= 2 errors in 0.1s =", (0, 2, 0)),
        ("= 1 failed, 1 error, 4 passed in 0.2s =", (4, 2, 0)),
        ("= no tests ran in 0.01s =", (0, 0, 0)),
    ],
    ids=["all-pass", "failures", "with-skips", "errors", "failures-and-errors", "none"],
)
def test_pytest_counts_are_parsed_deterministically(
    output: str, expected: tuple[int, int, int]
) -> None:
    outcome = parse_pytest_output(_result(output), side=ContractSide.NEW)

    assert (outcome.passed, outcome.failed, outcome.skipped) == expected


def test_failing_test_ids_are_extracted() -> None:
    output = (
        "FAILED tests/test_pay.py::test_charge - AssertionError\n"
        "ERROR tests/test_pay.py::test_setup\n"
        "= 1 failed, 1 error, 3 passed in 0.2s =\n"
    )

    outcome = parse_pytest_output(_result(output), side=ContractSide.NEW)

    assert outcome.failing_test_ids == [
        "tests/test_pay.py::test_charge",
        "tests/test_pay.py::test_setup",
    ]


def test_a_timed_out_suite_did_not_run() -> None:
    """A timeout tells us nothing about the code, and must not read as a pass."""
    outcome = parse_pytest_output(
        _result("= 4 passed in 1.0s =", timed_out=True), side=ContractSide.OLD
    )

    assert outcome.timed_out
    assert not outcome.ran


def test_selected_test_ids_are_deduplicated_and_ordered() -> None:
    """Both sides must run the same selectors in the same order.

    Otherwise the delta is between two different suites, and any difference it
    reports is an artefact of selection rather than of the contract.
    """

    class Node:
        def __init__(self, key: str) -> None:
            self.key = key

    nodes = [Node("tests/b.py"), Node("tests/a.py"), Node("tests/b.py")]

    assert selected_test_ids(nodes) == ["tests/a.py", "tests/b.py"]


# --- no model ------------------------------------------------------------


def test_the_rehearsal_invokes_no_model() -> None:
    """C6-04 acceptance: no result is ever model-generated.

    Structural, so it holds for code nobody has written yet. `test_results` is
    the table `02_ARCHITECTURE.md` §13 says a model may never write to, and this
    is the module that fills it.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[3] / "backend" / "validation" / "rehearsal.py"
    ).read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    offending = [
        module
        for module in imported
        if module.startswith(("strands", "backend.agents", "backend.shared.model_provider"))
    ]
    assert not offending, f"the rehearsal reaches a model: {offending}"


def test_a_rehearsal_result_summary_is_json_safe() -> None:
    """It is stored in a JSON column and read straight into the browser."""
    result = RehearsalResult(outcome=RunState.REHEARSAL_UNAVAILABLE, reason="because")

    assert json.loads(json.dumps(result.summary()))["outcome"] == "rehearsal_unavailable"
