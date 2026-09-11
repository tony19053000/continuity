"""C8-02: the human-control guarantee.

This is the file that has to hold when everything else fails. Its claims:

* an agent cannot approve anything, at any level of the stack;
* a protected action attempted on a PENDING approval is refused;
* an approval revoked between the grant and the action is caught, because the
  gate re-reads the row rather than trusting a decision made earlier;
* the approver recorded is the authenticated user and nobody else;
* the decision survives a restart, because it lives in the database.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from backend.api.app import create_app
from backend.api.auth.session import SESSION_COOKIE, issue_session
from backend.approvals.service import (
    ApprovalNotGranted,
    ApprovalRequest,
    create_request,
    require_granted,
    resolve,
)
from backend.models import (
    Approval,
    ApprovalStatus,
    Project,
    Repository,
    Severity,
    User,
)
from backend.models.session import create_all, session_scope
from backend.shared.config import Environment, GoogleOAuthConfig, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
SESSION_SECRET = SecretStr("approval-integrity-secret-value-for-tests")


@pytest.fixture
def approval_settings(tmp_path: Path) -> Settings:
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'approvals.db'}",
        GOOGLE_OAUTH_CLIENT_ID="test-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="test-client-secret",
        SESSION_SECRET=SESSION_SECRET,
        _env_file=None,
    )


@pytest.fixture
def google(approval_settings: Settings) -> GoogleOAuthConfig:
    config = approval_settings.google_oauth
    assert isinstance(config, GoogleOAuthConfig)
    return config


@pytest_asyncio.fixture(autouse=True)
async def database(approval_settings: Settings) -> AsyncIterator[None]:
    """One throwaway database per test, for HTTP and direct-session tests alike.

    Autouse because every test here touches approvals, and half of them never
    construct an HTTP client — those would otherwise run against no engine at
    all.
    """
    from backend.models.session import dispose_engine, init_engine

    init_engine(approval_settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


@pytest.fixture
def app(approval_settings: Settings) -> FastAPI:
    return create_app(approval_settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # No lifespan: the `database` fixture owns the engine, and running the
    # lifespan too would dispose it out from under the direct-session half of
    # each test.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _user(email: str) -> User:
    async with session_scope() as session:
        user = User(google_subject=f"sub-{uuid.uuid4()}", email=email)
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


async def _project(owner: User) -> Project:
    async with session_scope() as session:
        repository = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(user_id=owner.id, repository_id=repository.id, name="p")
        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project


async def _pending(project: Project, trigger: str = "install_dependency") -> Approval:
    async with session_scope() as session:
        approval = await create_request(
            session,
            ApprovalRequest(
                project_id=project.id,
                trigger=trigger,
                risk=Severity.HIGH,
                requested_action={"action": trigger, "package": "left-pad"},
            ),
        )
        await session.refresh(approval)
        return approval


def _as(client: AsyncClient, google: GoogleOAuthConfig, user: User) -> None:
    client.cookies.set(SESSION_COOKIE, issue_session(google, user.id))


# --- no agent can approve anything ---------------------------------------


def test_no_agent_module_can_reach_the_approval_service() -> None:
    """C8-02 acceptance, at the import graph.

    Import-level rather than behavioural: an agent that cannot import the
    service cannot call it however persuaded it becomes, and the rule holds for
    code nobody has written yet.
    """
    offenders = []
    for path in (BACKEND / "agents").rglob("*.py"):
        imports = {
            node.module
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        if any("approvals" in module for module in imports):
            offenders.append(path.relative_to(BACKEND).as_posix())

    assert not offenders, f"agent modules reaching the approval service: {offenders}"


def test_no_agent_module_can_reach_the_approval_api() -> None:
    """Nor the HTTP surface, nor FastAPI at all."""
    offenders = []
    for path in (BACKEND / "agents").rglob("*.py"):
        imports = {
            node.module
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        if any(module.startswith(("fastapi", "backend.api")) for module in imports):
            offenders.append(path.relative_to(BACKEND).as_posix())

    assert not offenders, f"agent modules reaching the API layer: {offenders}"


def test_the_only_writer_of_an_approval_decision_is_the_service() -> None:
    """One function sets `status` to anything but PENDING.

    `resolve` takes a `User` object, so a caller cannot supply an arbitrary id.
    The revoke endpoint is the one other writer and is listed here explicitly
    rather than being an exception nobody noticed.
    """
    import re

    writers = []
    setter = re.compile(r"\.status\s*=\s*ApprovalStatus\.")
    for path in BACKEND.rglob("*.py"):
        if setter.search(path.read_text()):
            writers.append(path.relative_to(BACKEND).as_posix())

    assert sorted(writers) == ["approvals/api.py", "approvals/service.py"]


async def test_an_agent_cannot_approve_its_own_request(client: AsyncClient) -> None:
    """C8-02 acceptance: the explicit self-approval attempt.

    There is no authenticated user, so there is no approver — and the endpoint
    refuses before it ever reaches the approval. An agent has no session, and a
    session is the only thing that can decide.
    """
    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)

    response = await client.post(f"/approvals/{approval.id}", json={"decision": "approve"})

    assert response.status_code == 401

    async with session_scope() as session:
        unchanged = await session.get(Approval, approval.id)
        assert unchanged is not None
        assert unchanged.status is ApprovalStatus.PENDING
        assert unchanged.actor_user_id is None


async def test_the_request_body_cannot_name_an_approver(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """C8-02 acceptance: only the authenticated approver's id is recorded.

    The body here tries to attribute the decision to someone else. The schema
    forbids unknown fields, and even if it did not, `actor_user_id` is taken
    from the session and never read from the request.
    """
    owner = await _user("owner@example.test")
    other = await _user("someone.else@example.test")
    project = await _project(owner)
    approval = await _pending(project)
    _as(client, google, owner)

    response = await client.post(
        f"/approvals/{approval.id}",
        json={"decision": "approve", "actor_user_id": str(other.id)},
    )

    assert response.status_code == 422, "an unknown field must be refused"

    response = await client.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    assert response.status_code == 200
    assert response.json()["actor_user_id"] == str(owner.id)


async def test_another_users_approval_is_not_visible_or_decidable(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """A 404 rather than a 403: confirming it exists would leak that it does."""
    owner = await _user("owner@example.test")
    intruder = await _user("intruder@example.test")
    project = await _project(owner)
    approval = await _pending(project)
    _as(client, google, intruder)

    assert (await client.get(f"/approvals/{approval.id}")).status_code == 404
    assert (
        await client.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    ).status_code == 404

    async with session_scope() as session:
        unchanged = await session.get(Approval, approval.id)
        assert unchanged is not None and unchanged.status is ApprovalStatus.PENDING


# --- the gate -------------------------------------------------------------


async def test_a_protected_action_on_a_pending_approval_is_refused() -> None:
    """C8-02 acceptance."""
    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)

    async with session_scope() as session:
        with pytest.raises(ApprovalNotGranted) as raised:
            await require_granted(session, approval.id, action="install_dependency")

    assert "pending" in str(raised.value)


async def test_a_rejected_approval_never_permits_the_action() -> None:
    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)

    async with session_scope() as session:
        await resolve(session, approval.id, actor=owner, approved=False)

    async with session_scope() as session:
        with pytest.raises(ApprovalNotGranted):
            await require_granted(session, approval.id, action="install_dependency")


async def test_an_approval_that_does_not_exist_permits_nothing() -> None:
    async with session_scope() as session:
        with pytest.raises(ApprovalNotGranted):
            await require_granted(session, uuid.uuid4(), action="install_dependency")


async def test_a_granted_approval_permits_the_action() -> None:
    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)

    async with session_scope() as session:
        await resolve(session, approval.id, actor=owner, approved=True)

    async with session_scope() as session:
        granted = await require_granted(session, approval.id, action="install_dependency")

    assert granted.status is ApprovalStatus.APPROVED
    assert granted.actor_user_id == owner.id


async def test_an_approval_revoked_between_grant_and_execution_is_caught(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """C8-02 acceptance: the revoke-between-grant-and-execute race.

    The run holds a session that read the approval while it was APPROVED. The
    person then revokes it through the HTTP API — a different session entirely.
    The gate must notice, which it only does because it re-reads the row
    immediately before the action rather than trusting what it read earlier.
    """
    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)
    _as(client, google, owner)

    granted = await client.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    assert granted.status_code == 200

    async with session_scope() as run_session:
        # The run checks, and is permitted.
        await require_granted(run_session, approval.id, action="install_dependency")

        # The person changes their mind, through a different session.
        revoked = await client.post(f"/approvals/{approval.id}/revoke")
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "rejected"

        # The run checks again immediately before acting, and is refused.
        with pytest.raises(ApprovalNotGranted):
            await require_granted(run_session, approval.id, action="install_dependency")


async def test_a_decision_is_final(client: AsyncClient, google: GoogleOAuthConfig) -> None:
    """Re-approving a rejected request would hide the rejection."""
    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)
    _as(client, google, owner)

    assert (
        await client.post(f"/approvals/{approval.id}", json={"decision": "reject"})
    ).status_code == 200

    second = await client.post(f"/approvals/{approval.id}", json={"decision": "approve"})

    assert second.status_code == 409
    assert second.json()["code"] == "approval_already_resolved"


async def test_only_an_approved_request_can_be_revoked(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)
    _as(client, google, owner)

    response = await client.post(f"/approvals/{approval.id}/revoke")

    assert response.status_code == 403


# --- durability -----------------------------------------------------------


async def test_approval_state_survives_a_restart(approval_settings: Settings) -> None:
    """C8-02 acceptance.

    The decision lives in the database, not in a process. Simulated by
    disposing the engine and initialising a new one against the same file —
    which is what a restart is.
    """
    from backend.models.session import dispose_engine, init_engine

    owner = await _user("owner@example.test")
    project = await _project(owner)
    approval = await _pending(project)

    async with session_scope() as session:
        await resolve(session, approval.id, actor=owner, approved=True)

    settings = Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=approval_settings.database_url,
        _env_file=None,
    )
    await dispose_engine()
    init_engine(settings)

    async with session_scope() as session:
        survived = await require_granted(session, approval.id, action="install_dependency")

    assert survived.status is ApprovalStatus.APPROVED
    assert survived.actor_user_id == owner.id
    assert survived.resolved_at is not None


# --- what the person sees -------------------------------------------------


async def test_a_requested_action_is_secret_filtered_on_the_way_out(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """The card is rendered in a browser, and the action came from an agent."""
    from tests.support.secret_samples import GITHUB_TOKEN

    owner = await _user("owner@example.test")
    project = await _project(owner)

    async with session_scope() as session:
        approval = await create_request(
            session,
            ApprovalRequest(
                project_id=project.id,
                trigger="migrate_credential",
                risk=Severity.HIGH,
                requested_action={"env": {"nested": {"TOKEN": GITHUB_TOKEN}}},
            ),
        )
        approval_id = approval.id

    _as(client, google, owner)
    response = await client.get(f"/approvals/{approval_id}")

    assert response.status_code == 200
    assert GITHUB_TOKEN not in response.text


async def test_pending_approvals_are_listed_for_their_owner_only(
    client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    owner = await _user("owner@example.test")
    intruder = await _user("intruder@example.test")
    project = await _project(owner)
    await _pending(project)

    _as(client, google, owner)
    mine = await client.get("/approvals")
    _as(client, google, intruder)
    theirs = await client.get("/approvals")

    assert len(mine.json()) == 1
    assert theirs.json() == []
