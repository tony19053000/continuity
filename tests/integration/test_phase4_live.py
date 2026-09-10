"""Phase 4 end to end against the live model.

Proves the chain the whole product rests on:

    repository → deterministic index → confirmed extraction
               → Gemini infers workflows → graph → baseline → blast radius

The stub-driven tests in `tests/unit/integrations/` prove the *contract* around
the agent. This proves the agent actually does the job — that a real model,
given real code, names workflows a person would recognise.

Skips with a named blocker when Gemini is unconfigured; never passes without it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.integrations.baseline import establish_baseline
from backend.integrations.graph import IntegrationGraph
from backend.integrations.mapper import map_integrations
from backend.models import Confidence, NodeKind, Project, Repository, RunState, User
from backend.models.session import session_scope
from backend.repository.indexer import build_index
from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.shared.config import GeminiConfig, Settings
from backend.shared.model_provider import build_model_provider

pytestmark = pytest.mark.requires_gemini


def _provider_or_skip():
    settings = Settings()
    gemini = settings.gemini
    if not isinstance(gemini, GeminiConfig):
        pytest.skip(
            "SKIPPED, NOT PASSED — Google Gemini is not configured: "
            f"{gemini.reason}. Workflow inference is unproven until this runs."
        )
    return build_model_provider(settings)


async def _make_project(session) -> Project:
    user = User(google_subject=f"s-{uuid.uuid4()}", email="live@example.test")
    session.add(user)
    await session.flush()
    repo = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
    session.add(repo)
    await session.flush()
    project = Project(
        user_id=user.id,
        repository_id=repo.id,
        name="commerce-api",
        state=RunState.INITIAL_SCAN_COMPLETE,
    )
    session.add(project)
    await session.flush()
    return project


async def test_a_real_model_infers_recognisable_business_workflows(
    database: None, sample_repo: Path
) -> None:
    """The judgment static analysis cannot supply.

    Asserted loosely on purpose: the exact wording is the model's, and pinning
    it would make this a brittle string test rather than a check that the
    inference is *useful*. What must hold is that a checkout-shaped workflow is
    found, that it is marked inferred, and that it cites real code.
    """
    provider = _provider_or_skip()
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=provider,
        )

        assert result.agent_skipped_reason is None, "the agent must have run"
        assert result.inferred_workflows > 0

        graph = IntegrationGraph(session, project.id)
        workflows = await graph.nodes(result.graph_version, kind=NodeKind.WORKFLOW)

        labels = " ".join(node.label.lower() for node in workflows)
        assert "checkout" in labels or "payment" in labels, (
            f"expected a payment-related workflow, got {[n.label for n in workflows]}"
        )

        for node in workflows:
            assert node.confidence is Confidence.INFERRED
            assert node.evidence is not None
            assert node.evidence["file_path"], "a workflow must cite a file"


async def test_the_full_chain_reaches_monitoring_with_a_real_baseline(
    database: None, sample_repo: Path
) -> None:
    """Provider → files → functions → workflows → tests, end to end, live."""
    provider = _provider_or_skip()
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=provider,
        )
        baseline = await establish_baseline(
            session, project, graph_version=result.graph_version
        )

        assert project.state is RunState.MONITORING_ACTIVE

        entry = next(e for e in baseline.entries if e.provider_id == "acmepay")
        assert entry.version == "v1"
        assert entry.call_sites == 4
        assert entry.workflows, "the live model produced no workflows"
        assert "tests/test_payments.py" in entry.tests


async def test_blast_radius_over_a_live_graph_finds_the_affected_code(
    database: None, sample_repo: Path
) -> None:
    """The query a provider change will run in Phase 6."""
    provider = _provider_or_skip()
    source = LocalRepositoryAdapter(sample_repo)

    async with session_scope() as session:
        project = await _make_project(session)
        result = await map_integrations(
            session,
            project_id=project.id,
            index=build_index(source),
            source=source,
            model_provider=provider,
        )

        graph = IntegrationGraph(session, project.id)
        call_sites = await graph.call_sites_for_provider(result.graph_version, "acmepay")
        radius = await graph.blast_radius(
            result.graph_version, {node.id for node in call_sites}
        )

    summary = radius.summary()
    assert "app/services/payment_service.py" in summary["files"]
    assert "create_payment()" in summary["symbols"]
    assert summary["workflows"], "no workflow reached from the call sites"
    assert "tests/test_payments.py" in summary["tests"]
