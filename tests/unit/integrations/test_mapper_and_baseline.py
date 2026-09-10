"""C4-03 and C4-04: the judgment layer, and the baseline it feeds.

The agent is driven by a stub runner here. What is under test is the *contract*
around it — that its output is marked inferred, that it cannot overwrite a
confirmed fact, that a hallucinated symbol key is dropped rather than persisted,
and that a failed agent degrades to the confirmed half instead of failing the
scan. None of that is model behaviour, and testing it through a live model would
be slow, flaky, and no more convincing.

The live path is proven separately in
`tests/integration/test_phase4_live.py`, which never passes without Gemini.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.agents.base import AgentOutputInvalid
from backend.agents.contracts import InferredWorkflow, IntegrationMapperOutput
from backend.integrations.baseline import describe_chain, establish_baseline
from backend.integrations.graph import IntegrationGraph
from backend.integrations.mapper import map_integrations
from backend.models import (
    Confidence,
    Integration,
    NodeKind,
    Project,
    ProviderBaseline,
    Repository,
    RunState,
    User,
)
from backend.models.session import session_scope
from backend.repository.indexer import build_index
from backend.repository.local_adapter import LocalRepositoryAdapter


class StubProvider:
    """Satisfies `ModelProvider` without reaching a network."""

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
        return self.outcome


def _workflows(*names: str) -> IntegrationMapperOutput:
    return IntegrationMapperOutput(
        workflows=[
            InferredWorkflow(
                name=name,
                basis=f"{name} basis",
                symbol_keys=["app/services/payment_service.py::create_payment"],
                evidence_file="app/services/payment_service.py",
                evidence_line_start=10,
                evidence_line_end=12,
            )
            for name in names
        ]
    )


@pytest.fixture
def patched_agent(monkeypatch: pytest.MonkeyPatch):
    """Install a stub runner into the mapper's agent."""

    def install(outcome: Any) -> StubRunner:
        runner = StubRunner(outcome)
        import backend.integrations.mapper as mapper_module

        original = mapper_module.IntegrationMapperAgent

        class Patched(original):  # type: ignore[valid-type,misc]
            def __init__(self, provider: Any, _runner: Any = None) -> None:
                super().__init__(provider, runner)

        monkeypatch.setattr(mapper_module, "IntegrationMapperAgent", Patched)
        return runner

    return install


async def _make_project(session, state: RunState = RunState.INITIAL_SCAN_COMPLETE) -> Project:
    user = User(google_subject=f"s-{uuid.uuid4()}", email="m@example.test")
    session.add(user)
    await session.flush()
    repo = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
    session.add(repo)
    await session.flush()
    project = Project(user_id=user.id, repository_id=repo.id, name="commerce-api", state=state)
    session.add(project)
    await session.flush()
    return project


# --- C4-03: the agent layer ---------------------------------------------


async def test_agent_workflows_are_persisted_as_inferred(
    database: None, sample_repo: Path, patched_agent
) -> None:
    patched_agent(_workflows("Checkout", "Subscription Renewal"))
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )

        graph = IntegrationGraph(session, project.id)
        workflows = await graph.nodes(result.graph_version, kind=NodeKind.WORKFLOW)

    assert {node.label for node in workflows} == {"Checkout", "Subscription Renewal"}
    assert all(node.confidence is Confidence.INFERRED for node in workflows)
    assert result.inferred_workflows == 2
    assert result.agent_skipped_reason is None


async def test_every_inferred_workflow_carries_evidence(
    database: None, sample_repo: Path, patched_agent
) -> None:
    patched_agent(_workflows("Checkout"))
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )
        graph = IntegrationGraph(session, project.id)
        workflow = await graph.node(result.graph_version, NodeKind.WORKFLOW, "Checkout")

    assert workflow is not None
    assert workflow.evidence is not None
    assert workflow.evidence["file_path"] == "app/services/payment_service.py"
    assert workflow.evidence["line_start"] == 10
    # Fixed by code, never by the agent.
    assert workflow.evidence["confidence"] == "inferred"


async def test_the_confirmed_half_survives_an_agent_failure(
    database: None, sample_repo: Path, patched_agent
) -> None:
    """A graph with facts and no workflow names is useful; losing the facts is not."""
    patched_agent(AgentOutputInvalid("model produced nothing usable"))
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )
        graph = IntegrationGraph(session, project.id)
        providers = await graph.providers(result.graph_version)

    assert result.confirmed_nodes > 0
    assert result.inferred_workflows == 0
    assert result.agent_skipped_reason is not None
    assert [p.key for p in providers] == ["acmepay"]


async def test_mapping_without_a_model_provider_still_produces_the_confirmed_graph(
    database: None, sample_repo: Path
) -> None:
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=None,
        )

    assert result.confirmed_nodes > 0
    assert result.inferred_workflows == 0
    assert result.agent_skipped_reason == "no model provider configured"


async def test_a_hallucinated_symbol_key_is_dropped_not_persisted(
    database: None, sample_repo: Path, patched_agent
) -> None:
    """A model naming a symbol that does not exist has invented a key.

    The workflow it proposed may still be right, so the workflow is kept and
    only the unresolvable edge is discarded.
    """
    output = IntegrationMapperOutput(
        workflows=[
            InferredWorkflow(
                name="Ghost",
                basis="references a symbol that does not exist",
                symbol_keys=["app/nowhere.py::does_not_exist"],
                evidence_file="app/nowhere.py",
                evidence_line_start=1,
                evidence_line_end=2,
            )
        ]
    )
    patched_agent(output)
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )
        graph = IntegrationGraph(session, project.id)
        workflow = await graph.node(result.graph_version, NodeKind.WORKFLOW, "Ghost")
        edges = await graph.edges(result.graph_version)

    assert workflow is not None, "the workflow itself should survive"
    assert not any(e.kind.value == "implements_workflow" for e in edges)


async def test_the_mapper_agent_holds_no_write_tool() -> None:
    from backend.agents.specialists import IntegrationMapperAgent

    assert IntegrationMapperAgent.contract.allowed_tools == frozenset()


# --- C4-04: baseline -----------------------------------------------------


async def test_the_baseline_records_versions_with_evidence(
    database: None, sample_repo: Path, patched_agent
) -> None:
    patched_agent(_workflows("Checkout"))
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )
        baseline = await establish_baseline(
            session, project, graph_version=result.graph_version
        )
        project_id = project.id

    assert [entry.provider_id for entry in baseline.entries] == ["acmepay"]
    assert baseline.entries[0].version == "v1"

    async with session_scope() as session:
        from sqlalchemy import select

        rows = (
            await session.execute(
                select(ProviderBaseline).where(ProviderBaseline.project_id == project_id)
            )
        ).scalars().all()

    assert len(rows) == 1
    assert rows[0].version == "v1"
    assert rows[0].evidence is not None, "a baseline claim must be checkable"


async def test_the_baseline_produces_the_documented_chain(
    database: None, sample_repo: Path, patched_agent
) -> None:
    """Provider → files → functions → workflows → tests → permissions."""
    patched_agent(_workflows("Checkout"))
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )
        baseline = await establish_baseline(
            session, project, graph_version=result.graph_version
        )

    entry = baseline.entries[0]
    assert entry.call_sites == 4
    assert "Checkout" in entry.workflows
    assert "tests/test_payments.py" in entry.tests

    rendered = describe_chain(baseline)
    assert "acmepay" in rendered
    assert "integration points" in rendered


async def test_the_project_reaches_monitoring_active(
    database: None, sample_repo: Path, patched_agent
) -> None:
    patched_agent(_workflows("Checkout"))
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )
        await establish_baseline(session, project, graph_version=result.graph_version)

        assert project.state is RunState.MONITORING_ACTIVE
        assert project.current_graph_version == result.graph_version


async def test_a_repository_with_no_integrations_still_reaches_monitoring(
    database: None, tmp_path: Path
) -> None:
    """No providers is a valid answer, not an error."""
    (tmp_path / "README.md").write_text("# nothing here\n")
    source = LocalRepositoryAdapter(tmp_path)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=None,
        )
        baseline = await establish_baseline(
            session, project, graph_version=result.graph_version
        )

        assert project.state is RunState.MONITORING_ACTIVE
        assert baseline.is_empty
        assert describe_chain(baseline) == "No external integrations detected."


async def test_the_integrations_summary_row_is_maintained(
    database: None, sample_repo: Path, patched_agent
) -> None:
    """What the Integrations page reads."""
    patched_agent(_workflows("Checkout"))
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=StubProvider(),
        )
        await establish_baseline(session, project, graph_version=result.graph_version)
        project_id = project.id

    async with session_scope() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(Integration).where(Integration.project_id == project_id)
            )
        ).scalar_one()

    assert row.provider_id == "acmepay"
    assert row.detected_api_version == "v1"
    assert row.integration_points == 4
    assert row.confidence is Confidence.CONFIRMED
