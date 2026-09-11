"""C9-04's backend half: the read surface the UI renders.

The frontend rule — no component contains a hardcoded provider, change,
workflow, test count, or status — only holds if the API supplies all of them.
These tests check that it does, and that it withholds what it does not have.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from backend.api.app import create_app
from backend.api.auth.session import SESSION_COOKIE, issue_session
from backend.integrations.graph import EdgeSpec, GraphDelta, IntegrationGraph, NodeSpec
from backend.models import (
    ChangeEvent,
    Integration,
    MigrationAttempt,
    MigrationRun,
    Project,
    Provider,
    PullRequest,
    Repository,
    RunState,
    SecurityFinding,
    User,
)
from backend.models.enums import (
    AttemptOutcome,
    ChangeType,
    Confidence,
    EdgeKind,
    EvidenceKind,
    FindingCategory,
    NodeKind,
    PolicyDecision,
    Severity,
)
from backend.models.schemas import Evidence
from backend.models.session import create_all, session_scope
from backend.shared.config import Environment, GoogleOAuthConfig, Settings

SESSION_SECRET = SecretStr("projects-api-secret-value-for-tests")


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        GOOGLE_OAUTH_CLIENT_ID="test-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="test-client-secret",
        SESSION_SECRET=SESSION_SECRET,
        _env_file=None,
    )


@pytest.fixture
def google(api_settings: Settings) -> GoogleOAuthConfig:
    config = api_settings.google_oauth
    assert isinstance(config, GoogleOAuthConfig)
    return config


@pytest_asyncio.fixture(autouse=True)
async def database(api_settings: Settings) -> AsyncIterator[None]:
    from backend.models.session import dispose_engine, init_engine

    init_engine(api_settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


@pytest_asyncio.fixture
async def client(api_settings: Settings) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(api_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _evidence(path: str = "app/client.py") -> Evidence:
    return Evidence(
        kind=EvidenceKind.SOURCE,
        confidence=Confidence.CONFIRMED,
        file_path=path,
        line_start=1,
        line_end=1,
    )


async def _seed(*, scanned: bool = True, covered: bool = True) -> tuple[User, Project]:
    """A project with one of everything the UI shows.

    Every unique-constrained value is suffixed, because seeding twice in one
    test is normal here and the change-event dedup key from C5-05 would
    otherwise (correctly) refuse the second one.
    """
    suffix = uuid.uuid4().hex[:8]
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="ui@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"commerce-api-{suffix}")
        session.add(repository)
        await session.flush()
        project = Project(
            user_id=user.id,
            repository_id=repository.id,
            name="commerce-api",
            state=RunState.MONITORING_ACTIVE,
        )
        session.add(project)
        await session.flush()

        session.add_all(
            [
                Integration(
                    project_id=project.id,
                    provider_id="acmepay",
                    display_name="AcmePay",
                    sdk_package="acmepay",
                    detected_api_version="v1",
                    auth_mechanism="oauth2",
                    integration_points=2,
                    confidence=Confidence.CONFIRMED,
                ),
            ]
        )

        # `providers` is global rather than per project, so a second seed reuses
        # the existing row instead of inserting a duplicate.
        existing = (
            await session.execute(
                select(Provider).where(Provider.provider_id == "acmepay")
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Provider(
                    provider_id="acmepay",
                    display_name="AcmePay",
                    adapter_name="acmepay",
                    last_checked_at=datetime.now(UTC),
                )
            )
        await session.flush()

        event = ChangeEvent(
            provider_id="acmepay",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.REQUEST_FIELD_REQUIRED,
            resource=f"POST /v1/charges request.currency {suffix}",
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
            state=RunState.MERGE_WAITING,
            target_branch="continuity/migrate-acmepay-v2",
        )
        session.add(run)
        await session.flush()

        session.add_all(
            [
                MigrationAttempt(
                    migration_run_id=run.id,
                    attempt_number=1,
                    outcome=AttemptOutcome.PASSED,
                ),
                SecurityFinding(
                    migration_run_id=run.id,
                    category=FindingCategory.OAUTH_SCOPE_CHANGE,
                    severity=Severity.HIGH,
                    summary="The patch requests customers.write.",
                    evidence={"kind": "source", "confidence": "confirmed"},
                    recommendation=PolicyDecision.ALLOW,
                    policy_decision=PolicyDecision.ASK,
                ),
                PullRequest(
                    migration_run_id=run.id,
                    repository_id=repository.id,
                    number=7,
                    url="https://github.com/acme/commerce-api/pull/7",
                    branch="continuity/migrate-acmepay-v2",
                    title="Migrate acmepay",
                    state="open",
                ),
            ]
        )

        if scanned:
            graph = IntegrationGraph(session, project.id)
            version = await graph.next_version()
            nodes = [
                NodeSpec(NodeKind.PROVIDER, "acmepay", "AcmePay", Confidence.CONFIRMED, _evidence()),
                NodeSpec(NodeKind.SYMBOL, "app/client.py::charge", "charge()", Confidence.CONFIRMED, _evidence()),
                NodeSpec(
                    NodeKind.CALL_SITE, "app/client.py:4:post", "post",
                    Confidence.CONFIRMED, _evidence(), {"resource": "/v1/charges"},
                ),
                NodeSpec(NodeKind.WORKFLOW, "Checkout", "Checkout", Confidence.INFERRED, _evidence()),
                NodeSpec(NodeKind.TEST, "tests/test_client.py", "tests/test_client.py", Confidence.CONFIRMED, _evidence()),
            ]
            edges = [
                EdgeSpec(EdgeKind.CALLS_PROVIDER, (NodeKind.CALL_SITE, "app/client.py:4:post"),
                         (NodeKind.PROVIDER, "acmepay"), Confidence.CONFIRMED, _evidence()),
                EdgeSpec(EdgeKind.DEFINED_IN, (NodeKind.CALL_SITE, "app/client.py:4:post"),
                         (NodeKind.SYMBOL, "app/client.py::charge"), Confidence.CONFIRMED, _evidence()),
                EdgeSpec(EdgeKind.IMPLEMENTS_WORKFLOW, (NodeKind.SYMBOL, "app/client.py::charge"),
                         (NodeKind.WORKFLOW, "Checkout"), Confidence.INFERRED, _evidence()),
            ]
            if covered:
                edges.append(
                    EdgeSpec(EdgeKind.COVERED_BY_TEST, (NodeKind.SYMBOL, "app/client.py::charge"),
                             (NodeKind.TEST, "tests/test_client.py"), Confidence.CONFIRMED, _evidence())
                )
            await graph.apply(GraphDelta(nodes=nodes, edges=edges), version=version)

        await session.flush()
        await session.refresh(user)
        await session.refresh(project)
        return user, project


def _as(client: AsyncClient, google: GoogleOAuthConfig, user: User) -> None:
    client.cookies.set(SESSION_COOKIE, issue_session(google, user.id))


# --- the surface exists ---------------------------------------------------


async def test_every_ui_endpoint_requires_authentication(client: AsyncClient) -> None:
    """Project data is a user's source code. None of it is public."""
    _, project = await _seed()

    for path in (
        "/projects",
        f"/projects/{project.id}",
        f"/projects/{project.id}/integrations",
        f"/projects/{project.id}/changes",
        f"/projects/{project.id}/runs",
        f"/projects/{project.id}/activity",
        f"/projects/{project.id}/graph",
        f"/projects/{project.id}/findings",
    ):
        response = await client.get(path)
        assert response.status_code == 401, path


async def test_another_users_project_is_not_visible(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """A 404, not a 403: confirming it exists would leak that it does."""
    _, project = await _seed()
    async with session_scope() as session:
        intruder = User(google_subject=f"s-{uuid.uuid4()}", email="x@example.test")
        session.add(intruder)
        await session.flush()
        await session.refresh(intruder)

    _as(client, google, intruder)

    assert (await client.get(f"/projects/{project.id}")).status_code == 404
    assert (await client.get("/projects")).json() == []


# --- what the screens read ------------------------------------------------


async def test_the_projects_list_carries_everything_the_grid_shows(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.5: name and state per project, from rows."""
    user, _ = await _seed()
    _as(client, google, user)

    body = (await client.get("/projects")).json()

    assert len(body) == 1
    summary = body[0]
    assert summary["name"] == "commerce-api"
    assert summary["state"] == "monitoring_active"
    assert summary["repository"].startswith("acme/commerce-api")
    assert summary["providers"] == 1
    assert summary["integration_points"] == 2
    assert summary["open_changes"] == 1


async def test_the_overview_carries_a_health_score_with_its_formula(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.6: the score links to its formula and its inputs.

    A number with no published arithmetic is not auditable, and this product's
    whole claim is that its figures are.
    """
    user, project = await _seed()
    _as(client, google, user)

    health = (await client.get(f"/projects/{project.id}")).json()["health"]

    assert health["available"] is True
    assert isinstance(health["score"], int)
    assert 0 <= health["score"] <= 100
    assert "coverage_penalty" in health["formula"]
    assert health["inputs"]["integration_points"] == 1
    assert health["inputs"]["relevant_unresolved_breaking"] == 1


async def test_an_unscanned_project_has_no_score_at_all(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.6: a score whose inputs are absent is not displayed.

    Showing a placeholder number would be a lie about system state, so the API
    refuses to produce one rather than leaving the decision to the UI.
    """
    user, project = await _seed(scanned=False)
    _as(client, google, user)

    health = (await client.get(f"/projects/{project.id}")).json()["health"]

    assert health["available"] is False
    assert health["score"] is None
    assert health["formula"] is None
    assert "has not been scanned" in health["unavailable_reason"]


async def test_uncovered_integration_points_lower_the_score(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """The coverage term is real arithmetic over graph edges, not a constant."""
    covered_user, covered_project = await _seed(covered=True)
    _as(client, google, covered_user)
    covered = (await client.get(f"/projects/{covered_project.id}")).json()["health"]

    uncovered_user, uncovered_project = await _seed(covered=False)
    _as(client, google, uncovered_user)
    uncovered = (await client.get(f"/projects/{uncovered_project.id}")).json()["health"]

    assert covered["inputs"]["uncovered_integration_points"] == 0
    assert uncovered["inputs"]["uncovered_integration_points"] == 1
    assert uncovered["score"] < covered["score"]


async def test_integrations_report_when_each_provider_was_last_checked(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.7. A provider never checked says so rather than showing a stale time."""
    user, project = await _seed()
    _as(client, google, user)

    (integration,) = (
        await client.get(f"/projects/{project.id}/integrations")
    ).json()

    assert integration["provider_id"] == "acmepay"
    assert integration["detected_api_version"] == "v1"
    assert integration["integration_points"] == 2
    assert integration["last_checked_at"] is not None
    assert integration["last_check_error"] is None


async def test_changes_link_to_the_run_they_opened(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.9. A change with no run is shown as such, not as a dead link."""
    user, project = await _seed()
    _as(client, google, user)

    (change,) = (await client.get(f"/projects/{project.id}/changes")).json()

    assert change["change_type"] == "request_field_required"
    assert change["breaking"] is True
    assert change["migration_run_id"] is not None


async def test_runs_carry_their_attempts_findings_and_pull_request(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.10 and §3.13, from counted rows rather than from a status string."""
    user, project = await _seed()
    _as(client, google, user)

    (run,) = (await client.get(f"/projects/{project.id}/runs")).json()

    assert run["state"] == "merge_waiting"
    assert run["attempts"] == 1
    assert run["findings"] == 1
    assert run["pull_request"] == 7
    assert run["target_branch"] == "continuity/migrate-acmepay-v2"


async def test_the_graph_carries_confidence_on_every_node_and_edge(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.8 and the confirmed/inferred rule.

    The UI cannot mark inferred data visibly if the API drops the marker, so it
    travels with every element.
    """
    user, project = await _seed()
    _as(client, google, user)

    graph = (await client.get(f"/projects/{project.id}/graph")).json()

    assert graph["version"] == 1
    assert graph["nodes"] and graph["edges"]
    assert all("confidence" in node for node in graph["nodes"])
    assert all("confidence" in edge for edge in graph["edges"])

    inferred = [n for n in graph["nodes"] if n["confidence"] == "inferred"]
    assert [n["label"] for n in inferred] == ["Checkout"]


async def test_an_unscanned_project_has_an_empty_graph_not_an_error(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    user, project = await _seed(scanned=False)
    _as(client, google, user)

    graph = (await client.get(f"/projects/{project.id}/graph")).json()

    assert graph == {"version": None, "nodes": [], "edges": []}


async def test_findings_expose_both_verdicts_and_the_disagreement(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.12. The security page shows what the agent said and what policy ruled."""
    user, project = await _seed()
    _as(client, google, user)

    (finding,) = (await client.get(f"/projects/{project.id}/findings")).json()

    assert finding["category"] == "oauth_scope_change"
    assert finding["recommendation"] == "allow"
    assert finding["policy_decision"] == "ask"
    assert finding["disagreed"] is True


async def test_the_report_endpoint_returns_the_pull_request_body(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """§3.11: the migration report a reviewer reads, from the same builder."""
    user, project = await _seed()
    _as(client, google, user)

    (run,) = (await client.get(f"/projects/{project.id}/runs")).json()
    body = (
        await client.get(f"/projects/{project.id}/runs/{run['id']}/report")
    ).json()

    assert "Continuity: acmepay v1 → v2" in body["markdown"]
    assert body["report"]["provider_id"] == "acmepay"
    assert body["report"]["pull_request"]["number"] == 7


async def test_a_run_belonging_to_another_project_is_not_readable(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    user_a, project_a = await _seed()
    _, project_b = await _seed()

    _as(client, google, user_a)

    # `user_a` cannot list project B at all, so read its run id directly.
    async with session_scope() as session:
        from sqlalchemy import select

        run_id = (
            await session.execute(
                select(MigrationRun.id).where(MigrationRun.project_id == project_b.id)
            )
        ).scalar_one()

    response = await client.get(f"/projects/{project_a.id}/runs/{run_id}/report")

    assert response.status_code == 404


# --- nothing leaks --------------------------------------------------------


async def test_no_endpoint_returns_a_secret(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """`04_FRONTEND_SPEC.md`: no secret reaches the browser."""
    from tests.support.secret_samples import GITHUB_TOKEN

    user, project = await _seed()

    async with session_scope() as session:
        from sqlalchemy import select

        finding = (
            await session.execute(select(SecurityFinding))
        ).scalars().first()
        assert finding is not None
        finding.summary = f"The patch added TOKEN={GITHUB_TOKEN}"
        await session.flush()

    _as(client, google, user)

    for path in (
        f"/projects/{project.id}",
        f"/projects/{project.id}/findings",
        f"/projects/{project.id}/runs",
    ):
        assert GITHUB_TOKEN not in (await client.get(path)).text, path


def test_the_read_api_cannot_reach_a_model() -> None:
    """A screen shows what happened, never what a model thinks happened."""
    import ast

    root = Path(__file__).resolve().parents[3] / "backend" / "api"
    offenders = []

    for path in root.rglob("*.py"):
        imports = {
            node.module
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        if any(
            module.startswith(("strands", "backend.agents", "backend.shared.model_provider"))
            for module in imports
        ):
            offenders.append(path.name)

    assert not offenders, f"API modules reaching a model: {offenders}"
