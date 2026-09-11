"""Phase 6 against the live model.

The stub-driven tests in `tests/unit/agents/test_impact_analyst.py` prove the
containment around the Impact Analyst. This proves the agent does the job — that
a real model, given a real change and the real code correlated to it, reaches
the conclusion a person would, and that the selectivity holds when the judge is
a live model rather than a stub that always says yes.

Skips with a named blocker when Gemini is unconfigured; never passes without it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from backend.agents.impact_analyst import evidence_confidence
from backend.integrations.correlation import Correlation, correlate_all
from backend.integrations.graph import IntegrationGraph
from backend.models import ChangeEvent, MigrationRun, Project, Repository, RunState, User
from backend.models.enums import Confidence
from backend.models.session import session_scope
from backend.orchestration.impact import assess_changes
from backend.shared.config import GeminiConfig, Settings
from backend.shared.model_provider import build_model_provider
from tests.support.correlation_fixtures import PROVIDER, change_set, fixture_graph

pytestmark = pytest.mark.requires_gemini


def _provider_or_skip():
    settings = Settings()
    gemini = settings.gemini
    if not isinstance(gemini, GeminiConfig):
        pytest.skip(
            "SKIPPED, NOT PASSED — Google Gemini is not configured: "
            f"{gemini.reason}. Impact judgment is unproven until this runs."
        )
    return build_model_provider(settings)


async def _project_with_graph() -> tuple[Project, int]:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="live@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(
            user_id=user.id,
            repository_id=repository.id,
            name="commerce-api",
            state=RunState.IMPACT_ANALYSIS_RUNNING,
        )
        session.add(project)
        await session.flush()

        graph = IntegrationGraph(session, project.id)
        version = await graph.next_version()
        await graph.apply(fixture_graph(), version=version)
        await session.refresh(project)
        return project, version


async def _correlated(
    project: Project, version: int
) -> list[tuple[ChangeEvent, Correlation]]:
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


@pytest.mark.usefixtures("database")
async def test_the_live_analyst_judges_a_real_change_set() -> None:
    """C6-03 end to end, with a live judge.

    Twelve changes. Nine reach no code and are settled deterministically — the
    model is never asked about them, which is asserted here in model calls
    rather than trusted. The three that do reach code are judged by Gemini, and
    the breaking one among them must come back relevant: the repository sends
    this request without the field that just became required.
    """
    provider = _provider_or_skip()
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)

    async with session_scope() as session:
        batch = await assess_changes(session, project, pairs, model_provider=provider)

    assert len(batch.assessments) == 12
    # The short-circuit holds against a live model: nine changes, no calls.
    assert batch.model_calls == 3

    by_resource = {
        next(e.resource for e, _ in pairs if e.id == a.change_event_id): a
        for a in batch.assessments
    }

    required_field = by_resource["POST /v1/charges request.currency"]
    assert required_field.decided_by == "impact_analyst"
    assert required_field.relevant, (
        "the model judged a newly required field on a called endpoint irrelevant: "
        f"{required_field.reasoning_summary}"
    )
    assert required_field.reasoning_summary

    # Everything the model was never asked about is settled, and settled as
    # irrelevant, by correlation alone.
    for resource in ("POST /v1/subscriptions", "GET /v2/disputes", "invoice.sent"):
        assert by_resource[resource].decided_by == "correlation"
        assert by_resource[resource].relevant is False


@pytest.mark.usefixtures("database")
async def test_live_affected_items_still_come_from_the_graph() -> None:
    """The model judges; the graph says what is affected.

    Worth proving through the live path and not only against a stub: this is
    where a real model's confident, plausible file list would otherwise reach a
    reviewer as though Continuity had read those files.
    """
    provider = _provider_or_skip()
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)

    async with session_scope() as session:
        batch = await assess_changes(session, project, pairs, model_provider=provider)

    assessed = next(a for a in batch.assessments if a.affected_files)

    assert [item.key for item in assessed.affected_files] == ["app/payments.py"]
    assert evidence_confidence(assessed) == {Confidence.CONFIRMED}
    for item in assessed.affected_symbols + assessed.affected_tests:
        assert item.evidence is not None


@pytest.mark.usefixtures("database")
async def test_a_live_run_opens_no_migration_for_an_unreached_change() -> None:
    """The row count, with a live judge rather than a cooperative stub."""
    provider = _provider_or_skip()
    project, version = await _project_with_graph()
    pairs = await _correlated(project, version)

    async with session_scope() as session:
        batch = await assess_changes(session, project, pairs, model_provider=provider)

    async with session_scope() as session:
        runs = (
            await session.execute(
                select(func.count()).select_from(MigrationRun).where(
                    MigrationRun.project_id == project.id
                )
            )
        ).scalar_one()

    # At most one run per correlated change, and never one for the other nine.
    assert runs <= 3
    assert runs == len([a for a in batch.assessments if a.migration_required])
    assert all(a.migration_run_id is None for a in batch.irrelevant)
