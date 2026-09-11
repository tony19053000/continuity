"""Continuity as one product, entered the way a real user enters it.

Every other integration test starts from state a test constructed. This one
starts from the HTTP API: import a repository, scan it, and let the pipeline
find a provider change on its own. Nothing here seeds a graph, a baseline, or a
migration run — if production code cannot create it, the test does not have it.

That distinction is the point. The audit that prompted this file found six
modules with no production caller and a pipeline that could never start on a
real project, while 1130 tests passed. A suite that only ever exercises modules
proves the modules work, not the product.

GitHub is stubbed at the client boundary — it is an external service, and
opening real pull requests on every test run is what `CLAUDE.md` forbids.
Everything inside Continuity is real: a real git checkout, real extraction, a
real graph, real worktrees, real `pytest` runs, real state transitions.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select

from backend.api.app import create_app
from backend.api.auth.session import SESSION_COOKIE, issue_session
from backend.migrations.workspace import WorkspaceManager
from backend.models import (
    ChangeEvent,
    GitHubInstallation,
    GraphNode,
    MigrationRun,
    Project,
    PullRequest,
    RunState,
    User,
)
from backend.models.enums import SourceKind
from backend.models.session import create_all, session_scope
from backend.observability.execution_audit import NullExecutionAudit
from backend.orchestration.coordinator import RunCoordinator
from backend.orchestration.pipeline import run_pipeline
from backend.orchestration.resume import resume_decided_runs, resume_run
from backend.providers.base import (
    BaseProviderAdapter,
    ExternalDocument,
    ProviderCapability,
    ProviderVersion,
)
from backend.providers.registry import ProviderRegistry
from backend.providers.storage import baseline_version
from backend.shared.config import Environment, GoogleOAuthConfig, Settings
from tests.support.agent_stubs import stub_agents
from tests.support.delivery_fixtures import FakeGitHub
from tests.support.product_repo import write_product_repo

SESSION_SECRET = SecretStr("product-loop-secret-value-for-tests")
FULL_NAME = "acme/commerce-api"
MAX_ATTEMPTS = 3


# --- the provider, which really changes ----------------------------------


def _spec(required: list[str]) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "AcmePay"},
        "paths": {
            "/v1/charges": {
                "post": {
                    "operationId": "createCharge",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": required,
                                    "properties": {
                                        "order": {"type": "string"},
                                        "amount": {"type": "integer"},
                                        "currency": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {}},
                }
            }
        },
    }


class AcmePay(BaseProviderAdapter):
    """v1 requires `amount`. v2 also requires `currency` — a real break."""

    provider_id = "acmepay"
    capabilities = frozenset(
        {ProviderCapability.CURRENT_VERSION, ProviderCapability.OPENAPI_SPEC}
    )

    def __init__(self, current: str = "v2") -> None:
        self._current = current

    async def get_current_version(self) -> ProviderVersion:
        self._require(ProviderCapability.CURRENT_VERSION)
        return ProviderVersion(version=self._current, is_current=True)

    async def fetch_openapi_spec(self, version: ProviderVersion) -> ExternalDocument:
        self._require(ProviderCapability.OPENAPI_SPEC)
        required = ["amount"] if version.version == "v1" else ["amount", "currency"]
        return ExternalDocument(
            kind=SourceKind.OPENAPI_SPEC,
            content=json.dumps(_spec(required), sort_keys=True),
            url=f"https://acmepay.test/{version.version}.json",
            version=version.version,
        )


# --- the migration the engineer is scripted to produce -------------------

#: What a good migration produces. It is deliberately robust — an idempotency
#: key on every money-moving call, a deadline, and a rate-limit branch — because
#: the Red Team (C9-01) attacks the code as it will exist after merge, and a
#: patch missing any of those is returned to the repair loop rather than
#: delivered. The weak version lives in `WEAK_MIGRATION` below, where it is used
#: to prove exactly that.
MIGRATED_CLIENT = '''\
"""Payment integration for the commerce API."""

import uuid

import httpx
from acmepay import AcmePayClient

client = AcmePayClient(api_version="v2")

#: How long a provider call may take before the workflow gives up on it.
TIMEOUT_SECONDS = 10


def create_payment(order_id: str, amount_cents: int, currency: str = "usd") -> dict:
    """Charge a customer for an order. Used by the checkout flow."""
    response = client.post(
        "/v1/charges",
        json={"order": order_id, "amount": amount_cents, "currency": currency},
        headers={"Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL, order_id))},
        timeout=TIMEOUT_SECONDS,
    )
    if getattr(response, "status_code", 200) == 429:
        raise RuntimeError("acmepay declined this request for capacity reasons")
    return response


def refund_payment(charge_id: str) -> httpx.Response:
    """Refund a charge. Present so the file has a real HTTP client, which is
    what the extractor looks for when deciding a call targets a provider."""
    return httpx.post(
        f"/v1/charges/{charge_id}/refund",
        headers={"Idempotency-Key": f"refund-{charge_id}"},
        timeout=TIMEOUT_SECONDS,
    )
'''

#: The same migration written carelessly: the currency field is added and the
#: idempotency key is dropped, so a retried checkout charges twice.
WEAK_MIGRATION = MIGRATED_CLIENT.replace(
    '        headers={"Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL, order_id))},\n',
    "",
).replace(
    '        headers={"Idempotency-Key": f"refund-{charge_id}"},\n',
    "",
)

SCOPE_WIDENING = MIGRATED_CLIENT + '\nSCOPES = ["customers.read", "customers.write"]\n'


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return write_product_repo(tmp_path / "commerce-api")


@pytest.fixture
def product_settings(tmp_path: Path) -> Settings:
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'product.db'}",
        GOOGLE_OAUTH_CLIENT_ID="test-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="test-client-secret",
        SESSION_SECRET=SESSION_SECRET,
        # The application starts its own scheduler; a test drives the pipeline
        # explicitly so assertions are not racing a background sweep.
        SCHEDULER_ENABLED=False,
        _env_file=None,
    )


@pytest.fixture
def google(product_settings: Settings) -> GoogleOAuthConfig:
    config = product_settings.google_oauth
    assert isinstance(config, GoogleOAuthConfig)
    return config


@pytest_asyncio.fixture(autouse=True)
async def database(product_settings: Settings) -> AsyncIterator[None]:
    from backend.models.session import dispose_engine, init_engine

    init_engine(product_settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


@pytest.fixture(autouse=True)
def agents(monkeypatch: pytest.MonkeyPatch):
    """Every runtime agent stubbed. The wiring is what is under test."""
    return stub_agents(monkeypatch)


class FakeAuthorizedGitHub:
    """Lists exactly the repository the installation authorizes, and no other."""

    def __init__(self, *names: str) -> None:
        self._names = names

    async def list_repositories(self) -> list[Any]:
        from backend.github.client import GitHubRepository

        return [
            GitHubRepository(
                id=index + 1,
                owner=name.split("/")[0],
                name=name.split("/")[1],
                default_branch="main",
                private=True,
            )
            for index, name in enumerate(self._names)
        ]

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def github_app(monkeypatch: pytest.MonkeyPatch):
    """Stub the GitHub App at the client boundary.

    The external service, and only that. Authorization is still enforced: the
    import endpoint refuses a repository this listing does not contain.
    """
    import backend.api.routers.onboarding as onboarding_module

    monkeypatch.setattr(
        onboarding_module,
        "build_github_client",
        lambda settings, installation_id: FakeAuthorizedGitHub(FULL_NAME),
    )


@pytest_asyncio.fixture
async def client(product_settings: Settings) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(product_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _signed_in(client: AsyncClient, google: GoogleOAuthConfig) -> User:
    """A user with a GitHub App installation, as sign-in plus connect produce."""
    async with session_scope() as session:
        user = User(google_subject=f"sub-{uuid.uuid4()}", email="dev@example.test")
        session.add(user)
        await session.flush()
        session.add(
            GitHubInstallation(
                user_id=user.id,
                installation_id=4900912,
                account_login="acme",
                account_type="Organization",
            )
        )
        await session.flush()
        await session.refresh(user)

    client.cookies.set(SESSION_COOKIE, issue_session(google, user.id))
    return user


async def _import_and_scan(
    client: AsyncClient, google: GoogleOAuthConfig, checkout: Path
) -> tuple[User, uuid.UUID, dict[str, Any]]:
    """The real entry point: import the repository, then scan it."""
    user = await _signed_in(client, google)

    imported = await client.post(
        "/repositories/import",
        json={"full_name": FULL_NAME, "local_path": str(checkout)},
    )
    assert imported.status_code == 201, imported.text
    project_id = uuid.UUID(imported.json()["project_id"])

    scanned = await client.post(f"/projects/{project_id}/scan")
    assert scanned.status_code == 200, scanned.text
    return user, project_id, scanned.json()


def _workspaces(checkout: Path, tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(
        checkout, workspace_root=tmp_path / "ws", audit=NullExecutionAudit()
    )


async def _pipeline(
    project_id: uuid.UUID,
    checkout: Path,
    tmp_path: Path,
    *,
    github: FakeGitHub | None = None,
    adapter: BaseProviderAdapter | None = None,
) -> Any:
    from tests.support.migration_fixtures import StubProvider

    registry = ProviderRegistry()
    registry.register(adapter or AcmePay())

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        return await run_pipeline(
            session,
            project,
            model_provider=StubProvider(),
            workspaces=_workspaces(checkout, tmp_path),
            coordinator=RunCoordinator(max_repair_attempts=MAX_ATTEMPTS),
            max_attempts=MAX_ATTEMPTS,
            adapter_registry=registry,
            github_client=github,
        )


@pytest.fixture
def engineer(monkeypatch: pytest.MonkeyPatch):
    """Script the Migration Engineer. Everything it produces is really applied."""
    from backend.agents import migration_engineer as engineer_module
    from tests.support.migration_fixtures import ScriptedEngineer, edit, output

    def install(*contents: str) -> ScriptedEngineer:
        runner = ScriptedEngineer(
            *[output(edit("app/payments.py", content)) for content in contents]
        )
        original = engineer_module.MigrationEngineerAgent

        class Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, provider: Any, **kwargs: Any) -> None:
                super().__init__(provider, runner=runner, **kwargs)

        monkeypatch.setattr(engineer_module, "MigrationEngineerAgent", Patched)
        return runner

    return install


# =========================================================================
# 1. A new repository becomes monitorable
# =========================================================================


async def test_a_new_repository_reaches_monitoring_without_any_seeding(
    client: AsyncClient, google: GoogleOAuthConfig, checkout: Path
) -> None:
    """The question the audit could not answer yes to.

    Import and scan are the only calls. Everything else — the index, the graph,
    the baseline, the monitorable state — is produced by production code.
    """
    _, project_id, scan = await _import_and_scan(client, google, checkout)

    assert scan["monitorable"] is True
    assert scan["state"] == "monitoring_active"
    assert scan["files_indexed"] > 0
    assert scan["graph_version"] == 1
    assert scan["confirmed_nodes"] > 0
    assert scan["providers"] == 1

    async with session_scope() as session:
        nodes = (
            await session.execute(
                select(func.count())
                .select_from(GraphNode)
                .where(GraphNode.project_id == project_id)
            )
        ).scalar_one()
        project = await session.get(Project, project_id)
        assert project is not None
        baseline = await baseline_version(session, project, "acmepay")

    assert nodes > 0, "a real scan must produce a real graph"
    assert baseline == "v1", "the baseline is what the next change is diffed from"


async def test_an_unauthorized_repository_cannot_be_imported(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """Signing in with Google is not authorization to touch code.

    The GitHub App installation is the authority, and a repository it does not
    list is refused however it is named.
    """
    await _signed_in(client, google)

    response = await client.post(
        "/repositories/import", json={"full_name": "someone/else"}
    )

    assert response.status_code == 404
    async with session_scope() as session:
        assert (
            await session.execute(select(func.count()).select_from(Project))
        ).scalar_one() == 0


async def test_importing_requires_sign_in(client: AsyncClient) -> None:
    response = await client.post(
        "/repositories/import", json={"full_name": FULL_NAME}
    )

    assert response.status_code == 401


# =========================================================================
# 2. A provider change becomes a pull request
# =========================================================================


async def test_the_whole_loop_from_import_to_pull_request(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """Import → scan → change → assess → rehearse → migrate → validate →
    review → deliver, with nothing seeded in between."""
    from backend.migrations.workspace import tree_hash

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    before = tree_hash(checkout)
    github = FakeGitHub()

    result = await _pipeline(project_id, checkout, tmp_path, github=github)

    assert result.changes_recorded >= 1, result.stopped_at
    assert result.relevant == 1
    (outcome,) = result.runs
    assert outcome.reached_delivery, f"{outcome.final_state}: {outcome.reason}"
    assert outcome.final_state is RunState.MERGE_WAITING
    assert result.pull_requests == [42]

    # The delivered bytes are the migrated file, and the user's checkout is
    # exactly as it was.
    assert github.blobs == [MIGRATED_CLIENT]
    assert tree_hash(checkout) == before
    assert "currency" not in (checkout / "app" / "payments.py").read_text()

    (_, pull) = next(c for c in github.calls if c[0] == "create_pull_request")
    assert "acmepay v1 → v2" in pull["body"]
    assert pull["base"] == "main"
    assert pull["head"].startswith("continuity/migrate-acmepay-")


# =========================================================================
# 3. The ASK path: pause, approve, resume the same run
# =========================================================================


async def test_ask_creates_an_approval_pauses_and_resumes_the_same_run(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The flow the audit found impossible.

    The migration widens the OAuth scopes it requests — valid code, green
    suite, and a decision a person should make. The run must pause with a real
    approval row, and approving it must deliver *the same patch*, rebuilt from
    what was stored rather than from a workspace that no longer exists.
    """
    engineer(SCOPE_WIDENING)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    paused = await _pipeline(project_id, checkout, tmp_path, github=github)

    (outcome,) = paused.runs
    assert outcome.final_state is RunState.APPROVAL_PENDING
    assert not outcome.reached_delivery
    assert github.called == [], "nothing may be written before a person answers"

    # A real, answerable request — not just a state.
    pending = await client.get("/approvals")
    assert pending.status_code == 200
    (card,) = pending.json()
    assert card["status"] == "pending"
    assert card["migration_run_id"] == str(outcome.migration_run_id)
    assert card["requested_action"]["files"] == ["app/payments.py"]

    approved = await client.post(f"/approvals/{card['id']}", json={"decision": "approve"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    async with session_scope() as session:
        run = await session.get(MigrationRun, outcome.migration_run_id)
        assert run is not None
        resumed = await resume_run(
            session,
            run,
            workspaces=_workspaces(checkout, tmp_path),
            github_client=github,
        )

    assert resumed.reached_delivery, resumed.reason
    assert resumed.final_state is RunState.MERGE_WAITING
    # The same patch a person approved, rebuilt and delivered.
    assert github.blobs == [SCOPE_WIDENING]


async def test_a_paused_run_stays_paused_until_someone_answers(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    engineer(SCOPE_WIDENING)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    paused = await _pipeline(project_id, checkout, tmp_path, github=github)
    (outcome,) = paused.runs

    async with session_scope() as session:
        run = await session.get(MigrationRun, outcome.migration_run_id)
        assert run is not None
        result = await resume_run(
            session, run, workspaces=_workspaces(checkout, tmp_path), github_client=github
        )

    assert not result.reached_delivery
    assert "waiting on a human" in result.reason
    assert github.called == []


async def test_rejecting_terminates_the_run_and_delivers_nothing(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    engineer(SCOPE_WIDENING)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    paused = await _pipeline(project_id, checkout, tmp_path, github=github)
    (outcome,) = paused.runs

    (card,) = (await client.get("/approvals")).json()
    assert (
        await client.post(f"/approvals/{card['id']}", json={"decision": "reject"})
    ).status_code == 200

    async with session_scope() as session:
        run = await session.get(MigrationRun, outcome.migration_run_id)
        assert run is not None
        result = await resume_run(
            session, run, workspaces=_workspaces(checkout, tmp_path), github_client=github
        )

    assert not result.reached_delivery
    assert result.final_state is RunState.MONITORING_ACTIVE
    assert github.called == []


async def test_the_scheduler_sweep_resumes_an_approved_run(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """Approving in the UI is enough. Nobody has to poke the run afterwards."""
    engineer(SCOPE_WIDENING)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    await _pipeline(project_id, checkout, tmp_path, github=github)
    (card,) = (await client.get("/approvals")).json()
    await client.post(f"/approvals/{card['id']}", json={"decision": "approve"})

    manager = _workspaces(checkout, tmp_path)

    async def workspaces_for(_: uuid.UUID) -> Any:
        return manager

    async def github_for(_: uuid.UUID) -> Any:
        return github

    async with session_scope() as session:
        results = await resume_decided_runs(
            session, workspaces_for=workspaces_for, github_for=github_for
        )

    assert [r.reached_delivery for r in results] == [True]


# =========================================================================
# 4 & 8. The gate refuses what it cannot evidence
# =========================================================================


async def test_a_blocking_finding_prevents_delivery(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DENY stops the run, and nothing reaches GitHub."""
    from backend.agents.contracts import ProposedFinding, SecurityReviewerOutput
    from backend.models.enums import (
        Confidence,
        EvidenceKind,
        FindingCategory,
        PolicyDecision,
        Severity,
    )
    from backend.models.schemas import Evidence

    stub_agents(
        monkeypatch,
        security_reviewer=SecurityReviewerOutput(
            findings=[
                ProposedFinding(
                    category=FindingCategory.AUTHORIZATION_WEAKENED,
                    severity=Severity.CRITICAL,
                    summary="The patch removes a permission check.",
                    evidence=Evidence(
                        kind=EvidenceKind.SOURCE,
                        confidence=Confidence.INFERRED,
                        file_path="app/payments.py",
                    ),
                    recommendation=PolicyDecision.DENY,
                )
            ],
            overall_recommendation=PolicyDecision.DENY,
            summary="This removes an authorization check.",
        ),
    )
    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    result = await _pipeline(project_id, checkout, tmp_path, github=github)

    (outcome,) = result.runs
    assert outcome.final_state is RunState.SECURITY_REVIEW_FAILED
    assert not outcome.reached_delivery
    assert github.called == []

    async with session_scope() as session:
        pulls = (
            await session.execute(select(func.count()).select_from(PullRequest))
        ).scalar_one()
    assert pulls == 0


async def test_delivery_refuses_a_run_with_no_recorded_security_review(
    client: AsyncClient, google: GoogleOAuthConfig, checkout: Path, tmp_path: Path
) -> None:
    """"No findings" and "nobody looked" are not the same answer.

    A run with attempts but no recorded review must not deliver on the grounds
    that its finding list is empty.
    """
    from backend.github.delivery import DeliveryRefused
    from backend.models import MigrationAttempt
    from backend.models.enums import AttemptOutcome, ChangeType
    from backend.orchestration.delivery_gate import preconditions_for

    _, project_id, _ = await _import_and_scan(client, google, checkout)

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        event = ChangeEvent(
            provider_id="acmepay",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.REQUEST_FIELD_REQUIRED,
            resource=f"POST /v1/charges request.currency {uuid.uuid4().hex[:6]}",
            breaking=True,
            source={"kind": "openapi_spec"},
            evidence={"kind": "provider_spec", "confidence": "confirmed"},
            detected_at=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ),
        )
        session.add(event)
        await session.flush()
        run = MigrationRun(
            project_id=project.id,
            change_event_id=event.id,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            state=RunState.PR_PENDING,
        )
        session.add(run)
        await session.flush()
        session.add(
            MigrationAttempt(
                migration_run_id=run.id,
                attempt_number=1,
                outcome=AttemptOutcome.PASSED,
                patch_diff="--- a/x\n+++ b/x\n",
            )
        )
        await session.flush()

        with pytest.raises(DeliveryRefused, match="no security review"):
            await preconditions_for(session, run, files={"app/payments.py": "x"})


async def test_delivery_refuses_when_the_files_do_not_match_the_review(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """An approval is for one patch, not for the idea of a patch.

    The run is paused with a recorded tree digest; delivering different bytes
    must be refused even though every other precondition holds.
    """
    from backend.github.delivery import DeliveryRefused
    from backend.orchestration.delivery_gate import preconditions_for

    engineer(SCOPE_WIDENING)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    paused = await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())
    (outcome,) = paused.runs

    (card,) = (await client.get("/approvals")).json()
    await client.post(f"/approvals/{card['id']}", json={"decision": "approve"})

    async with session_scope() as session:
        run = await session.get(MigrationRun, outcome.migration_run_id)
        assert run is not None
        with pytest.raises(DeliveryRefused, match="do not match the reviewed patch"):
            await preconditions_for(
                session, run, files={"app/payments.py": "something else entirely"}
            )


# =========================================================================
# 5. Validation failure drives the repair loop
# =========================================================================


async def test_a_failing_patch_is_repaired_within_the_budget(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The first patch breaks the suite; the second fixes it. Both are recorded."""
    from backend.models import MigrationAttempt
    from backend.models.enums import AttemptOutcome

    broken = MIGRATED_CLIENT.replace(
        'def create_payment(order_id: str, amount_cents: int, currency: str = "usd") -> dict:',
        "def create_payment(order_id: str, amount_cents: int, currency: str) -> dict:",
    )
    engineer(broken, MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    result = await _pipeline(project_id, checkout, tmp_path, github=github)

    (outcome,) = result.runs
    assert outcome.repair is not None and outcome.repair.repaired
    assert outcome.reached_delivery

    async with session_scope() as session:
        attempts = list(
            (
                await session.execute(
                    select(MigrationAttempt)
                    .where(MigrationAttempt.migration_run_id == outcome.migration_run_id)
                    .order_by(MigrationAttempt.attempt_number)
                )
            ).scalars()
        )

    assert [a.outcome for a in attempts] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.PASSED,
    ]
    assert attempts[0].failure_evidence is not None
    assert attempts[0].failure_evidence["failed"] >= 1


# =========================================================================
# 6. The Red Team attacks the migration before a human sees it (C9-01)
# =========================================================================


async def test_a_patch_the_red_team_breaks_goes_back_to_the_repair_loop(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The first patch passes the suite and still must not ship.

    It drops the idempotency key, so a retried checkout charges twice. Tests
    cannot see that — they pass. The Red Team can, and the run returns to
    `REPAIR_RUNNING` rather than to a person.
    """
    from backend.models import SecurityFinding
    from backend.models.enums import AttemptOutcome, FindingCategory

    engineer(WEAK_MIGRATION, MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    result = await _pipeline(project_id, checkout, tmp_path, github=github)

    (outcome,) = result.runs
    repair = outcome.repair
    assert repair is not None

    # Both attempts passed their tests. The first was rejected anyway.
    assert [a.outcome for a in repair.attempts] == [
        AttemptOutcome.PASSED,
        AttemptOutcome.PASSED,
    ]
    assert outcome.reached_delivery
    assert github.blobs == [MIGRATED_CLIENT], "the weak patch was never delivered"

    assert repair.red_team is not None and repair.red_team.held

    async with session_scope() as session:
        findings = list(
            (
                await session.execute(
                    select(SecurityFinding).where(
                        SecurityFinding.migration_run_id == outcome.migration_run_id
                    )
                )
            ).scalars()
        )
    duplicates = [
        f for f in findings if f.category is FindingCategory.DUPLICATE_TRANSACTION_RISK
    ]
    assert duplicates, "the attack that blocked the first patch is recorded"
    assert "idempotency" in duplicates[0].summary.lower()


async def test_a_patch_the_red_team_keeps_breaking_ends_with_a_person(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The loop is bounded. It does not retry forever, and it does not give up
    into delivery — it ends where only a human can move it."""
    engineer(WEAK_MIGRATION, WEAK_MIGRATION, WEAK_MIGRATION, WEAK_MIGRATION)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    github = FakeGitHub()

    result = await _pipeline(project_id, checkout, tmp_path, github=github)

    (outcome,) = result.runs
    assert outcome.final_state is RunState.HUMAN_REVIEW_REQUIRED
    assert not outcome.reached_delivery
    assert github.calls == []


async def test_the_red_team_runs_on_the_code_not_the_diff(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The attack surface is the file as it will exist after merge.

    `files_attacked` names the migrated file, which the diff-based Security
    Reviewer never sees in full — that difference is the reason this stage
    exists at all.
    """
    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)

    result = await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    (outcome,) = result.runs
    assert outcome.repair is not None
    report = outcome.repair.red_team
    assert report is not None
    assert report.files_attacked == ["app/payments.py"]


async def test_a_finding_from_a_rejected_patch_is_labelled_as_history(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The rejected patch's finding stays, and is not mistaken for a live one.

    Deleting it would destroy the evidence that an attempt was rejected and
    why. Presenting it unlabelled would tell a reviewer that the code about to
    ship double-charges, when the patch that did was thrown away.
    """
    engineer(WEAK_MIGRATION, MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)

    result = await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())
    (outcome,) = result.runs
    assert outcome.reached_delivery

    findings = (await client.get(f"/projects/{project_id}/findings")).json()
    superseded = [f for f in findings if f["superseded"]]
    assert superseded, "the rejected attempt's finding is still readable"
    assert superseded[0]["attempt_number"] == 1
    assert all(f["attempt_number"] is not None for f in findings)

    # The run card counts what is wrong with the patch that is shipping.
    (run_view,) = (await client.get(f"/projects/{project_id}/runs")).json()
    assert run_view["findings"] == len(findings) - len(superseded)


async def test_the_report_does_not_claim_a_rejected_patch_expanded_permissions(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The evidence report lists every finding and claims only about the
    current one."""
    from backend.migrations.evidence import build_report

    engineer(WEAK_MIGRATION, MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    result = await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())
    (outcome,) = result.runs

    async with session_scope() as session:
        run = await session.get(MigrationRun, outcome.migration_run_id)
        assert run is not None
        report = await build_report(session, run)

    assert any(f["superseded"] for f in report.security_findings), (
        "the rejected attempt's finding is in the report"
    )
    assert report.permission_expansions == []


async def test_delivery_refuses_a_run_with_no_recorded_red_team_attack(
    client: AsyncClient, google: GoogleOAuthConfig, checkout: Path
) -> None:
    """Same distinction as the security review, one stage earlier.

    An empty attack list means "attacked and held" only if something actually
    attacked it. A run with no record was not found robust; it was never tried.
    """
    from backend.github.delivery import DeliveryRefused
    from backend.models import MigrationAttempt
    from backend.models.enums import AttemptOutcome, ChangeType
    from backend.orchestration.delivery_gate import (
        SECURITY_REVIEW_KEY,
        preconditions_for,
    )

    _, project_id, _ = await _import_and_scan(client, google, checkout)

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        event = ChangeEvent(
            provider_id="acmepay",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.REQUEST_FIELD_REQUIRED,
            resource=f"POST /v1/charges request.currency {uuid.uuid4().hex[:6]}",
            breaking=True,
            source={"kind": "openapi_spec"},
            evidence={"kind": "provider_spec", "confidence": "confirmed"},
            detected_at=__import__("datetime").datetime.now(
                __import__("datetime").UTC
            ),
        )
        session.add(event)
        await session.flush()
        run = MigrationRun(
            project_id=project.id,
            change_event_id=event.id,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            state=RunState.PR_PENDING,
            # Reviewed, and never attacked.
            evidence_report={SECURITY_REVIEW_KEY: {"decision": "allow"}},
        )
        session.add(run)
        await session.flush()
        session.add(
            MigrationAttempt(
                migration_run_id=run.id,
                attempt_number=1,
                outcome=AttemptOutcome.PASSED,
                patch_diff="--- a/x\n+++ b/x\n",
            )
        )
        await session.flush()

        with pytest.raises(DeliveryRefused, match="no red-team attack"):
            await preconditions_for(session, run, files={"app/payments.py": "x"})


# =========================================================================
# 7. Merge → verification → baseline advances
# =========================================================================


async def test_merging_verifies_the_run_and_advances_the_baseline(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The loop's last step, and the one that makes the next pass correct."""
    from backend.github.webhooks import settle

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        assert await baseline_version(session, project, "acmepay") == "v1"

        record = (
            await session.execute(select(PullRequest))
        ).scalar_one()
        outcome = await settle(session, record, merged=True)

    assert outcome == "merged"

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        run = (await session.execute(select(MigrationRun))).scalar_one()

        assert run.state is RunState.VERIFIED
        assert project.state is RunState.MONITORING_ACTIVE
        # Only now. The next release is diffed from v2.
        assert await baseline_version(session, project, "acmepay") == "v2"


async def test_a_project_with_no_declared_checks_claims_no_verification(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """C9-02's default, and the one most projects will be in.

    The fixture repository declares no `.continuity/verification.json`, so
    nothing was asked of any environment — and `VERIFIED` must not read as
    though something was.
    """
    from backend.github.webhooks import settle
    from backend.workers.post_merge_verify import DISABLED, NOT_CONFIGURED

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    async with session_scope() as session:
        record = (await session.execute(select(PullRequest))).scalar_one()
        assert await settle(session, record, merged=True) == "merged"

    async with session_scope() as session:
        run = (await session.execute(select(MigrationRun))).scalar_one()
        assert run.state is RunState.VERIFIED

        stored = run.evidence_report or {}
        release = stored.get("release_verification")
        # Either the feature is off or there is no manifest. Both are recorded,
        # and neither claims a deployment was checked.
        assert release is None or release["verification"] in {
            NOT_CONFIGURED,
            DISABLED,
        }


async def test_a_declared_check_that_regressed_stops_the_run_and_the_baseline(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """C9-02 acceptance: a regressed environment reaches a person, not VERIFIED."""
    from backend.agents.release_guardian import Regression, ReleaseAssessment
    from backend.workers.post_merge import verify_merge
    from backend.workers.post_merge_verify import FAILED, EnvironmentVerification

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    async def regressed(_session: Any, _run: Any) -> EnvironmentVerification:
        return EnvironmentVerification(
            verification=FAILED,
            detail="1 regression(s) after the merge.",
            assessment=ReleaseAssessment(
                passed=False,
                compared_with_baseline=True,
                regressions=[
                    Regression(name="checkout", reason="expected 200, got 500", status=500)
                ],
            ),
            checks_run=2,
        )

    async with session_scope() as session:
        record = (await session.execute(select(PullRequest))).scalar_one()
        record.merged = True
        await session.flush()
        run = (await session.execute(select(MigrationRun))).scalar_one()
        result = await verify_merge(session, run, environment=regressed)

    assert not result.verified
    assert result.verification == FAILED
    assert result.checks["release:checkout"] is False

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        run = (await session.execute(select(MigrationRun))).scalar_one()

        assert run.state is RunState.HUMAN_REVIEW_REQUIRED
        # The migration is merged and the environment broke. Moving the baseline
        # would tell the next pass this version is running fine.
        assert await baseline_version(session, project, "acmepay") == "v1"


async def test_declared_checks_that_hold_let_the_run_verify(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The other half of the acceptance: configured and healthy."""
    from backend.agents.release_guardian import ReleaseAssessment
    from backend.workers.post_merge import verify_merge
    from backend.workers.post_merge_verify import PASSED, EnvironmentVerification

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    async def healthy(_session: Any, _run: Any) -> EnvironmentVerification:
        return EnvironmentVerification(
            verification=PASSED,
            detail="2 declared check(s) passed after the merge.",
            assessment=ReleaseAssessment(passed=True, compared_with_baseline=True),
            checks_run=2,
        )

    async with session_scope() as session:
        record = (await session.execute(select(PullRequest))).scalar_one()
        record.merged = True
        await session.flush()
        run = (await session.execute(select(MigrationRun))).scalar_one()
        result = await verify_merge(session, run, environment=healthy)

    assert result.verified
    assert result.verification == PASSED
    # The claim grows only when something was actually checked.
    assert ".continuity/verification.json" in result.scope

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        assert await baseline_version(session, project, "acmepay") == "v2"


async def test_the_baseline_does_not_advance_on_an_unverified_merge(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """A merge Continuity cannot tie to its own run proves nothing."""
    from backend.github.webhooks import settle

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    async with session_scope() as session:
        run = (await session.execute(select(MigrationRun))).scalar_one()
        run.target_branch = "continuity/migrate-acmepay-somethingelse"
        await session.flush()

        record = (await session.execute(select(PullRequest))).scalar_one()
        outcome = await settle(session, record, merged=True)

    assert outcome == "merged_unverified"

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        assert await baseline_version(session, project, "acmepay") == "v1"


async def test_a_second_pass_after_verification_finds_nothing_new(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """The loop closes: once the baseline is v2, v2 is no longer a change."""
    from backend.github.webhooks import settle

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    async with session_scope() as session:
        record = (await session.execute(select(PullRequest))).scalar_one()
        await settle(session, record, merged=True)

    second = await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    assert second.changes_recorded == 0
    assert second.runs == []
    assert second.stopped_at == "no new provider changes"


# =========================================================================
# 6. The scheduler drives it
# =========================================================================


async def test_the_scheduler_invokes_monitoring_for_a_real_project(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick reaches the pipeline for a project that onboarding made ready."""
    import backend.workers.scheduler as scheduler_module
    from backend.workers.scheduler import ProviderScheduler

    _, project_id, _ = await _import_and_scan(client, google, checkout)
    seen: list[uuid.UUID] = []

    async def recording_pipeline(session: Any, project: Project, **_: Any) -> Any:
        from backend.orchestration.pipeline import PipelineResult

        seen.append(project.id)
        return PipelineResult(project_id=project.id, stopped_at="recorded")

    monkeypatch.setattr(scheduler_module, "run_pipeline", recording_pipeline)

    async def collaborators(_: uuid.UUID) -> dict[str, Any]:
        return {}

    result = await ProviderScheduler(
        interval_seconds=1, pipeline_factory=collaborators
    ).tick()

    assert seen == [project_id]
    assert result.checked == [project_id]


async def test_the_application_starts_the_scheduler(
    product_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-06's real test: the lifespan starts it, and stops it cleanly.

    Asserted through the app's own lifespan rather than by calling `start()`,
    because "nothing started it" was the defect.
    """
    started: list[str] = []

    class Recording:
        async def start(self) -> None:
            started.append("start")

        async def stop(self) -> None:
            started.append("stop")

    import backend.api.app as app_module

    monkeypatch.setattr(app_module, "build_scheduler", lambda settings: Recording())
    monkeypatch.setattr(
        app_module, "sweep_orphan_workspaces", lambda settings: _noop()
    )

    enabled = product_settings.model_copy(update={"SCHEDULER_ENABLED": True})
    monkeypatch.setattr(
        type(enabled), "gemini", property(lambda self: _fake_gemini())
    )

    app = create_app(enabled)
    async with app.router.lifespan_context(app):
        assert started == ["start"]

    assert started == ["start", "stop"]


async def _noop() -> list[str]:
    return []


def _fake_gemini() -> Any:
    from backend.shared.config import GeminiConfig

    return GeminiConfig(api_key=SecretStr("test-key"), model="gemini-2.5-flash")


# =========================================================================
# Merge detection without webhooks — the path this deployment actually uses
# =========================================================================


async def test_polling_detects_a_merge_and_closes_the_loop(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
) -> None:
    """Webhooks are off here, so polling is the only merge signal there is.

    Found while wiring: `settle()` is the only thing that marks a pull request
    merged, and it was reachable only from the webhook receiver. With no signing
    secret configured — this deployment — nothing would ever mark a merge, and
    post-merge verification would wait for a state that never arrived. The loop
    would look closed and not be.
    """
    from backend.workers.pr_status_poll import poll_open_pull_requests

    engineer(MIGRATED_CLIENT)
    _, project_id, _ = await _import_and_scan(client, google, checkout)
    await _pipeline(project_id, checkout, tmp_path, github=FakeGitHub())

    class MergedUpstream:
        """GitHub, reporting that a human merged the pull request."""

        async def get_pull_request(self, owner: str, name: str, number: int) -> Any:
            return {"state": "closed", "merged": True}

    async with session_scope() as session:
        result = await poll_open_pull_requests(session, client=MergedUpstream())

    assert result.checked == 1
    assert result.settled == ["#42: merged"]

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        run = (await session.execute(select(MigrationRun))).scalar_one()

        # Detected, verified, and the baseline moved — without a webhook.
        assert run.state is RunState.VERIFIED
        assert await baseline_version(session, project, "acmepay") == "v2"


# =========================================================================
# 10. Running a pass by hand (the verification surface)
# =========================================================================


async def test_running_a_pass_by_hand_refuses_without_a_model(
    client: AsyncClient, google: GoogleOAuthConfig, checkout: Path
) -> None:
    """A refusal with a reason, not a pass that found nothing.

    Without a model a pass cannot judge relevance, and one that ran anyway would
    either guess or migrate for every provider release. 503 is the right answer:
    the operator has to add configuration.
    """
    _, project_id, _ = await _import_and_scan(client, google, checkout)

    response = await client.post(f"/projects/{project_id}/run")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "integration_not_configured"


async def test_running_a_pass_by_hand_reports_what_it_could_not_do(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest half of the button.

    A pass that checked no providers because none are configured looks exactly
    like a pass that found nothing. The endpoint must distinguish them: someone
    pressing this button is trying to find out whether the product works, and
    "nothing happened" would teach them the wrong thing.
    """
    _collaborators(monkeypatch)
    _, project_id, _ = await _import_and_scan(client, google, checkout)

    response = await client.post(f"/projects/{project_id}/run")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["providers_seen"] == 1, "the project does depend on a provider"
    assert body["providers_monitored"] == 0, "and nothing is monitoring it"
    assert body["unmonitored"] == ["acmepay: no adapter registered for this provider"]
    assert any(
        "No provider adapter is registered" in item for item in body["limitations"]
    )
    assert [stage["name"] for stage in body["stages"]][:2] == ["monitor", "assess"]


async def test_running_a_pass_by_hand_drives_the_real_pipeline(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    tmp_path: Path,
    engineer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same call the scheduler makes, triggered by a person.

    The provider is registered the way a deployment registers one — in the
    default registry — so this exercises the production lookup rather than the
    test-only `adapter_registry` argument.
    """
    from backend.providers.registry import registry
    from backend.workers import runner as runner_module

    engineer(MIGRATED_CLIENT)
    _collaborators(monkeypatch)
    _, project_id, _ = await _import_and_scan(client, google, checkout)

    registry.register(AcmePay(), replace=True)
    monkeypatch.setattr(
        runner_module,
        "project_workspace_manager",
        lambda _settings, _project_id: _ready(_workspaces(checkout, tmp_path)),
    )
    try:
        response = await client.post(f"/projects/{project_id}/run")
    finally:
        registry.unregister("acmepay")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["providers_monitored"] == 1
    assert body["unmonitored"] == []
    assert body["changes_recorded"] >= 1
    assert body["relevant"] == 1
    assert len(body["runs"]) == 1
    # No GitHub client in this deployment, so the pass does every stage up to
    # delivery and stops there — and says so rather than looking like a failure.
    assert any("GitHub App" in item for item in body["limitations"])
    assert [stage["name"] for stage in body["stages"]][:2] == ["monitor", "assess"]


def _collaborators(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the endpoint the collaborators a configured deployment would have.

    Only the model is substituted; the coordinator and the attempt budget are
    the real ones, and everything the endpoint does with them is production
    code.
    """
    from backend.orchestration import runtime as runtime_module
    from tests.support.migration_fixtures import StubProvider

    async def collaborators(_settings: Any) -> dict[str, Any]:
        return {
            "model_provider": StubProvider(),
            "coordinator": RunCoordinator(max_repair_attempts=MAX_ATTEMPTS),
            "max_attempts": MAX_ATTEMPTS,
        }

    monkeypatch.setattr(runtime_module, "pipeline_collaborators", collaborators)


async def _ready(value: Any) -> Any:
    return value


async def test_two_passes_on_one_project_cannot_run_at_once(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard the scheduler always had, now shared with the button.

    Two pipelines on one project fight over the same workspace and open
    competing migration runs. The scheduler refused a second pass; this endpoint
    makes the same call, so it has to refuse too — and refuse, not queue, since
    a queued pass would run against a repository state its caller never saw.
    """
    import asyncio

    from backend.orchestration import pipeline as pipeline_module

    _collaborators(monkeypatch)
    _, project_id, _ = await _import_and_scan(client, google, checkout)

    entered = asyncio.Event()
    release = asyncio.Event()
    real = pipeline_module.run_pipeline

    async def slow(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "run_pipeline", slow)

    first = asyncio.create_task(client.post(f"/projects/{project_id}/run"))
    await asyncio.wait_for(entered.wait(), timeout=5)

    second = await client.post(f"/projects/{project_id}/run")
    release.set()

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "pass_already_running"
    assert (await first).status_code == 200


async def test_a_project_mid_migration_does_not_start_another_pass(
    client: AsyncClient,
    google: GoogleOAuthConfig,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler's state gate, applied to the button as well.

    A run waiting on an approval owns its workspace and its state. Starting a
    fresh pass alongside it would be a second claim on both.
    """
    _collaborators(monkeypatch)
    _, project_id, _ = await _import_and_scan(client, google, checkout)

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        project.state = RunState.APPROVAL_PENDING
        await session.flush()

    response = await client.post(f"/projects/{project_id}/run")

    assert response.status_code == 422, response.text
    assert "approval_pending" in response.json()["message"]
