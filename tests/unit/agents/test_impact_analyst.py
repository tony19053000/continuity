"""C6-03: deciding whether a provider change matters here.

The agent is driven by a stub runner. What is under test is the containment
around it — that an irrelevant change never reaches a model at all, that "what
is affected" comes from the graph rather than from the model, that an
irrelevant change cannot open a migration run, and that the agent holds no
write tools. None of that is model behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.agents import impact_analyst as impact_module
from backend.agents.contracts import ImpactAnalystOutput
from backend.agents.impact_analyst import assess_change, evidence_confidence
from backend.integrations.correlation import Correlation, correlate_all
from backend.integrations.graph import IntegrationGraph
from backend.models import (
    ActivityEvent,
    ChangeEvent,
    MigrationRun,
    Project,
    Repository,
    RunState,
    User,
)
from backend.models.enums import AgentRole, ChangeType, Confidence, Severity
from backend.models.session import session_scope
from backend.orchestration.impact import assess_changes
from tests.support.correlation_fixtures import PROVIDER, _change, change_set, fixture_graph

pytestmark = pytest.mark.usefixtures("database")


class StubProvider:
    @property
    def model_id(self) -> str:
        return "stub"

    def build_model(self, role: Any) -> Any:
        return object()


class StubRunner:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0

    async def run_structured(self, **_: Any) -> Any:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(self.calls)
        return self.outcome


@pytest.fixture
def patched_agent(monkeypatch: pytest.MonkeyPatch):
    def install(outcome: Any) -> StubRunner:
        runner = StubRunner(outcome)
        original = impact_module.ImpactAnalystAgent

        class Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, provider: Any, **kwargs: Any) -> None:
                super().__init__(provider, runner=runner, **kwargs)

        monkeypatch.setattr(impact_module, "ImpactAnalystAgent", Patched)
        return runner

    return install


def _output(
    *,
    relevant: bool = True,
    migration_required: bool = True,
    severity: Severity = Severity.HIGH,
    affected_files: list[str] | None = None,
) -> ImpactAnalystOutput:
    return ImpactAnalystOutput(
        relevant=relevant,
        severity=severity,
        migration_required=migration_required,
        affected_files=affected_files if affected_files is not None else ["app/payments.py"],
        affected_symbols=["app/payments.py::charge"],
        affected_workflows=["Checkout"],
        affected_tests=["tests/test_payments.py"],
        reasoning_summary="Two call sites send this request without the field.",
    )


async def _project_with_graph() -> tuple[Project, int]:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="i@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(
            user_id=user.id,
            repository_id=repository.id,
            name="p",
            state=RunState.IMPACT_ANALYSIS_RUNNING,
        )
        session.add(project)
        await session.flush()

        graph = IntegrationGraph(session, project.id)
        version = await graph.next_version()
        await graph.apply(fixture_graph(), version=version)
        await session.refresh(project)
        return project, version


async def _correlated(project: Project, version: int) -> list[tuple[ChangeEvent, Correlation]]:
    """The fixture change set, persisted and correlated."""
    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlations = await correlate_all(graph, version, PROVIDER, change_set())

        pairs = []
        for correlation in correlations:
            event = ChangeEvent(
                provider_id=PROVIDER,
                old_version="v1",
                new_version="v2",
                change_type=correlation.change.change_type,
                resource=correlation.change.resource,
                breaking=correlation.change.breaking,
                source=correlation.change.source.model_dump(mode="json"),
                evidence=correlation.change.evidence.model_dump(mode="json"),
                detected_at=datetime.now(UTC),
            )
            session.add(event)
            await session.flush()
            pairs.append((event, correlation))
        return pairs


# --- the deterministic short-circuit -------------------------------------


async def test_a_change_reaching_no_code_never_reaches_the_model(
    patched_agent: Any,
) -> None:
    """There is no judgment to make about a change that touches nothing.

    Sending it anyway would invite the model to find impact that is not there,
    and would spend a model call on every irrelevant release a provider ships.
    """
    runner = patched_agent(_output())
    empty = Correlation(change=_change(ChangeType.ENDPOINT_REMOVED, "POST /v1/nothing"))

    assessment, used_model = await assess_change(
        empty, uuid.uuid4(), project_id=uuid.uuid4(), model_provider=StubProvider()
    )

    assert used_model is False
    assert runner.calls == 0
    assert assessment.relevant is False
    assert assessment.migration_required is False
    assert assessment.decided_by == "correlation"
    assert assessment.severity is Severity.INFO


async def test_only_the_correlated_changes_cost_a_model_call(patched_agent: Any) -> None:
    """C6-03's majority-irrelevant fixture, measured in model calls.

    Twelve changes, three of which reach code. Nine model calls saved is also
    nine opportunities for a model to talk itself into a migration.
    """
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    runner = patched_agent(_output(relevant=False, migration_required=False))

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    assert len(batch.assessments) == 12
    assert batch.model_calls == 3
    assert runner.calls == 3


# --- relevance and migration runs ----------------------------------------


async def test_the_expected_relevant_subset_is_identified(patched_agent: Any) -> None:
    """C6-03 acceptance: exactly the expected subset, not approximately."""
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    patched_agent(_output())

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    relevant_resources = sorted(
        next(e.resource for e, _ in pairs if e.id == a.change_event_id)
        for a in batch.relevant
    )

    assert relevant_resources == [
        "GET /v1/refunds/{id} response.amount",
        "POST /v1/charges request.currency",
        "payment.paid",
    ]
    assert len(batch.irrelevant) == 9


async def test_no_migration_run_is_created_for_an_irrelevant_change(
    patched_agent: Any,
) -> None:
    """C6-03 acceptance, asserted by counting rows.

    The count is the honest measure: an implementation that assessed correctly
    and opened a run anyway would pass every other assertion here.
    """
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    patched_agent(_output())

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    async with session_scope() as session:
        runs = (
            await session.execute(
                select(func.count()).select_from(MigrationRun).where(
                    MigrationRun.project_id == project.id
                )
            )
        ).scalar_one()

    assert runs == 3
    assert batch.runs_created == 3
    assert all(a.migration_run_id is None for a in batch.irrelevant)
    assert all(a.migration_run_id is not None for a in batch.relevant)


async def test_a_release_that_affects_nothing_opens_no_run_at_all(
    patched_agent: Any,
) -> None:
    """The case that has to be cheap, because it is the usual one."""
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    runner = patched_agent(_output(relevant=False, migration_required=False))

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    async with session_scope() as session:
        runs = (
            await session.execute(select(func.count()).select_from(MigrationRun))
        ).scalar_one()
        refreshed = await session.get(Project, project.id)

    assert runs == 0
    assert batch.runs_created == 0
    assert runner.calls == 3
    assert refreshed is not None
    assert refreshed.state is RunState.CHANGE_IRRELEVANT


async def test_an_irrelevant_change_cannot_require_a_migration(
    patched_agent: Any,
) -> None:
    """"Irrelevant, but migrate" is not a coherent answer.

    The conjunction is enforced in code rather than trusted to the model,
    because this is the single output combination that would open a migration
    run against a change the model just said does not apply.
    """
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    patched_agent(_output(relevant=False, migration_required=True))

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    async with session_scope() as session:
        runs = (
            await session.execute(select(func.count()).select_from(MigrationRun))
        ).scalar_one()

    assert runs == 0
    assert all(not a.migration_required for a in batch.assessments)


async def test_a_relevant_change_that_needs_no_migration_opens_no_run(
    patched_agent: Any,
) -> None:
    """Worth telling someone about is not the same as worth patching."""
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    patched_agent(_output(relevant=True, migration_required=False, severity=Severity.LOW))

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    async with session_scope() as session:
        runs = (
            await session.execute(select(func.count()).select_from(MigrationRun))
        ).scalar_one()
        refreshed = await session.get(Project, project.id)

    assert runs == 0
    assert len(batch.relevant) == 3
    assert refreshed is not None
    assert refreshed.state is RunState.CHANGE_RELEVANT


# --- evidence ------------------------------------------------------------


async def test_every_affected_item_carries_confirmed_evidence(
    patched_agent: Any,
) -> None:
    """C6-03 acceptance.

    CONFIRMED because the items come from graph nodes the extractor wrote, not
    from the model. An `Evidence` object a model assembles is an assertion about
    a file; one taken from a node is a record of having read it.
    """
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    patched_agent(_output())

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    assessed = next(a for a in batch.relevant if a.affected_symbols)

    assert assessed.affected_files and assessed.affected_symbols
    assert assessed.affected_workflows and assessed.affected_tests
    for group in (
        assessed.affected_files,
        assessed.affected_symbols,
        assessed.affected_tests,
    ):
        for item in group:
            assert item.evidence is not None, f"{item.key} carries no evidence"
    assert evidence_confidence(assessed) == {Confidence.CONFIRMED}


async def test_affected_items_come_from_the_graph_not_from_the_model(
    patched_agent: Any,
) -> None:
    """A file the model invents never reaches a report.

    The model is asked for its own account and it is recorded — but the
    authoritative set is the graph's, so a confident hallucination is dropped
    and counted rather than shown to a reviewer as an affected file.
    """
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    patched_agent(
        _output(affected_files=["app/payments.py", "app/does_not_exist.py", "/etc/passwd"])
    )

    async with session_scope() as session:
        batch = await assess_changes(
            session, project, pairs, model_provider=StubProvider()
        )

    assessed = next(a for a in batch.relevant if a.affected_files)

    assert [item.key for item in assessed.affected_files] == ["app/payments.py"]
    assert assessed.dropped_unknown == ["/etc/passwd", "app/does_not_exist.py"]


async def test_a_relevant_change_records_an_activity_event(patched_agent: Any) -> None:
    """The run timeline is a direct read of this table."""
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)
    patched_agent(_output())

    async with session_scope() as session:
        await assess_changes(session, project, pairs, model_provider=StubProvider())

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ActivityEvent).where(ActivityEvent.project_id == project.id)
            )
        ).scalars().all()

    impacted = [r for r in rows if r.kind.value == "impacted_workflow_detected"]
    assert len(impacted) == 3
    assert all(r.actor == "impact_analyst" for r in impacted)
    assert all(r.evidence is not None for r in impacted)


# --- degradation ---------------------------------------------------------


async def test_without_a_model_a_correlated_change_escalates_rather_than_passing() -> None:
    """The absence of a judge is not evidence of safety.

    Code that calls a changed endpoint is a fact established deterministically.
    Reporting it as irrelevant because no model was reachable would turn an
    outage into a silent one.
    """
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)

    async with session_scope() as session:
        batch = await assess_changes(session, project, pairs, model_provider=None)

    async with session_scope() as session:
        runs = (
            await session.execute(select(func.count()).select_from(MigrationRun))
        ).scalar_one()
        refreshed = await session.get(Project, project.id)

    assert batch.model_calls == 0
    assert len(batch.relevant) == 3
    # Relevant, but no migration is opened on an unjudged change.
    assert runs == 0
    assert all(not a.migration_required for a in batch.relevant)
    assert refreshed is not None
    assert refreshed.state is RunState.CHANGE_RELEVANT


async def test_changes_that_reach_nothing_are_still_irrelevant_without_a_model() -> None:
    """The deterministic answer does not depend on the model being reachable."""
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)

    async with session_scope() as session:
        batch = await assess_changes(session, project, pairs, model_provider=None)

    assert len(batch.irrelevant) == 9
    assert all(a.decided_by == "correlation" for a in batch.irrelevant)


# --- authority -----------------------------------------------------------


def test_the_impact_analyst_holds_no_tools() -> None:
    """C6-03 acceptance: it cannot modify code."""
    from backend.agents.specialists import SPECIALISTS

    assert SPECIALISTS[AgentRole.IMPACT_ANALYST].contract.allowed_tools == frozenset()


def test_the_impact_analyst_cannot_create_a_migration_run() -> None:
    """Opening a run is the consequence of a judgment, never the judgment itself.

    `assess_changes` writes the row after applying the relevance conjunction.
    The agent module the analyst runs in has no table import at all.
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "backend" / "agents" / "specialists.py"
    ).read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not [m for m in imported if "models.tables" in m or m == "backend.models"]
