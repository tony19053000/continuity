"""C8-06: merge detection, and why a verified delivery is still untrusted.

A webhook endpoint is the one unauthenticated door into Continuity. These tests
are written from that position: what can someone who can reach this URL make it
do?
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from backend.api.app import create_app
from backend.github.webhooks import (
    SIGNATURE_HEADER,
    expected_signature,
    signature_matches,
)
from backend.models import (
    AuditEvent,
    ChangeEvent,
    MigrationRun,
    Project,
    PullRequest,
    Repository,
    RunState,
    User,
)
from backend.models.enums import ChangeType
from backend.models.session import create_all, session_scope
from backend.shared.config import Environment, Settings

WEBHOOK_SECRET = "a-webhook-signing-secret-for-tests"
PRIVATE_KEY = Path("/nonexistent/key.pem")


@pytest.fixture
def hook_settings(tmp_path: Path) -> Settings:
    """A deployment with webhooks configured."""
    key = tmp_path / "key.pem"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n-----END RSA PRIVATE KEY-----\n")
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'hooks.db'}",
        GITHUB_APP_ID="4900912",
        GITHUB_APP_CLIENT_ID="test-client-id",
        GITHUB_APP_CLIENT_SECRET=SecretStr("test-client-secret"),
        GITHUB_APP_PRIVATE_KEY_PATH=str(key),
        GITHUB_APP_WEBHOOK_SECRET=SecretStr(WEBHOOK_SECRET),
        _env_file=None,
    )


@pytest.fixture
def unconfigured_settings(tmp_path: Path) -> Settings:
    """A deployment with webhooks intentionally disabled — the current one."""
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'nohooks.db'}",
        _env_file=None,
    )


@pytest_asyncio.fixture
async def database(hook_settings: Settings) -> AsyncIterator[None]:
    from backend.models.session import dispose_engine, init_engine

    init_engine(hook_settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


@pytest_asyncio.fixture
async def client(hook_settings: Settings, database: None) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(hook_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _open_pull_request(
    number: int = 7, *, repo_name: str = "app"
) -> tuple[PullRequest, MigrationRun, Repository]:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="w@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=repo_name)
        session.add(repository)
        await session.flush()
        project = Project(
            user_id=user.id,
            repository_id=repository.id,
            name="p",
            state=RunState.MERGE_WAITING,
        )
        session.add(project)
        await session.flush()
        event = ChangeEvent(
            provider_id="acmepay",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.REQUEST_FIELD_REQUIRED,
            resource=f"r-{uuid.uuid4().hex[:6]}",
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
            # Set by delivery on a real run. Post-merge verification checks the
            # merged branch against it, so a fixture without one is a run that
            # cannot be verified.
            target_branch="continuity/migrate-acmepay-v2",
        )
        session.add(run)
        await session.flush()
        record = PullRequest(
            migration_run_id=run.id,
            repository_id=repository.id,
            number=number,
            url=f"https://github.com/acme/{repo_name}/pull/{number}",
            branch="continuity/migrate-acmepay-v2",
            title="Migrate acmepay",
            state="open",
        )
        session.add(record)
        await session.flush()
        await session.refresh(record)
        await session.refresh(run)
        await session.refresh(repository)
        return record, run, repository


def _payload(number: int = 7, *, merged: bool = True, state: str = "closed") -> bytes:
    return json.dumps(
        {
            "action": "closed",
            "repository": {"full_name": "acme/app"},
            "pull_request": {"number": number, "state": state, "merged": merged},
        }
    ).encode()


def _headers(body: bytes, *, secret: str = WEBHOOK_SECRET, event: str = "pull_request") -> dict[str, str]:
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(uuid.uuid4()),
        SIGNATURE_HEADER: expected_signature(secret, body),
        "Content-Type": "application/json",
    }


# --- signature verification ----------------------------------------------


def test_a_correct_signature_matches() -> None:
    body = _payload()

    assert signature_matches(WEBHOOK_SECRET, body, expected_signature(WEBHOOK_SECRET, body))


@pytest.mark.parametrize(
    "provided",
    [
        None,
        "",
        "sha256=" + "0" * 64,
        "garbage",
        # Right digest, wrong prefix — a naive `in` or `endswith` would pass it.
        hash_only := None,
    ],
    ids=["missing", "empty", "wrong-digest", "garbage", "prefixless"],
)
def test_a_bad_signature_never_matches(provided: str | None) -> None:
    body = _payload()
    if provided is None and hash_only is None:
        provided = expected_signature(WEBHOOK_SECRET, body).removeprefix("sha256=")

    assert not signature_matches(WEBHOOK_SECRET, body, provided)


def test_a_signature_from_the_wrong_secret_never_matches() -> None:
    body = _payload()

    assert not signature_matches(
        WEBHOOK_SECRET, body, expected_signature("a-different-secret", body)
    )


def test_a_signature_over_different_bytes_never_matches() -> None:
    """The signature covers the body, so a tampered body invalidates it."""
    signature = expected_signature(WEBHOOK_SECRET, _payload(merged=True))

    assert not signature_matches(WEBHOOK_SECRET, _payload(merged=False), signature)


def test_the_comparison_is_constant_time() -> None:
    """C8-06 acceptance, asserted at the source.

    `==` on a digest leaks how much of the prefix was right, one byte at a time.
    This is not observable from a behavioural test, so it is checked structurally.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "backend" / "github" / "webhooks.py"
    ).read_text()

    assert "compare_digest" in source
    tree = ast.parse(source)
    in_signature_fn = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "signature_matches"
    ]
    assert in_signature_fn
    comparisons = [
        node for node in ast.walk(in_signature_fn[0]) if isinstance(node, ast.Compare)
    ]
    for comparison in comparisons:
        assert not any(isinstance(op, ast.Eq) for op in comparison.ops), (
            "signature_matches uses == on a digest"
        )


# --- the endpoint ---------------------------------------------------------


async def test_an_unsigned_delivery_is_refused_and_audited(client: AsyncClient) -> None:
    """C8-06 acceptance."""
    _, run, _ = await _open_pull_request()
    body = _payload()

    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
    )

    assert response.status_code == 401

    async with session_scope() as session:
        audits = list((await session.execute(select(AuditEvent))).scalars())
        unchanged = await session.get(MigrationRun, run.id)

    assert [a.kind for a in audits] == ["webhook.refused"]
    assert audits[0].detail["reason"] == "signature mismatch"
    assert unchanged is not None and unchanged.state is RunState.MERGE_WAITING


async def test_a_mis_signed_delivery_is_refused(client: AsyncClient) -> None:
    _, run, _ = await _open_pull_request()
    body = _payload()

    response = await client.post(
        "/webhooks/github", content=body, headers=_headers(body, secret="wrong-secret")
    )

    assert response.status_code == 401

    async with session_scope() as session:
        unchanged = await session.get(MigrationRun, run.id)
    assert unchanged is not None and unchanged.state is RunState.MERGE_WAITING


async def test_the_provided_signature_is_never_stored(client: AsyncClient) -> None:
    """It is attacker-supplied, so recording it puts chosen bytes in the audit."""
    await _open_pull_request()
    body = _payload()
    chosen = "sha256=" + "deadbeef" * 8

    await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            SIGNATURE_HEADER: chosen,
            "Content-Type": "application/json",
        },
    )

    async with session_scope() as session:
        audit = (await session.execute(select(AuditEvent))).scalars().first()

    assert audit is not None
    assert "deadbeef" not in json.dumps(audit.detail)


async def test_a_merged_pull_request_verifies_the_run(client: AsyncClient) -> None:
    """C8-06 acceptance: merged → verification → VERIFIED.

    A merge no longer goes straight to VERIFIED. Post-merge verification checks
    the merge is consistent with this run, and only then advances the baseline —
    which is what makes the next pass diff from the right side.
    """
    record, run, _ = await _open_pull_request()
    body = _payload(merged=True)

    response = await client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 202
    assert response.json()["status"] == "merged"

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)
        pull = await session.get(PullRequest, record.id)
        project = await session.get(Project, run.project_id)

    assert refreshed is not None and refreshed.state is RunState.VERIFIED
    assert pull is not None and pull.merged is True and pull.state == "closed"
    assert pull.merged_at is not None
    assert project is not None and project.state is RunState.MONITORING_ACTIVE

    # The point of verifying: the baseline moved, so the next release is diffed
    # from v2 rather than from v1 forever.
    async with session_scope() as session:
        from backend.providers.storage import baseline_version

        refreshed_project = await session.get(Project, run.project_id)
        assert refreshed_project is not None
        assert (
            await baseline_version(session, refreshed_project, "acmepay") == "v2"
        )


async def test_a_merge_inconsistent_with_the_run_is_not_verified(
    client: AsyncClient,
) -> None:
    """A merge on a branch this run did not create proves nothing.

    The baseline must not move on it: Continuity would then believe the project
    runs against a version nothing verified it against, and the next real break
    would be invisible.
    """
    from backend.providers.storage import baseline_version

    _, run, _ = await _open_pull_request()

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        tracked.target_branch = "continuity/migrate-acmepay-v9"
        await session.flush()

    body = _payload(merged=True)
    response = await client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.json()["status"] == "merged_unverified"

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)
        project = await session.get(Project, run.project_id)
        assert project is not None
        assert await baseline_version(session, project, "acmepay") is None

    assert refreshed is not None
    assert refreshed.state is RunState.HUMAN_REVIEW_REQUIRED


async def test_a_closed_unmerged_pull_request_returns_to_monitoring(
    client: AsyncClient,
) -> None:
    """C8-06 acceptance. A rejected migration is not a failure; it is an answer."""
    record, run, _ = await _open_pull_request()
    body = _payload(merged=False)

    response = await client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.json()["status"] == "closed"

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)
        pull = await session.get(PullRequest, record.id)

    assert refreshed is not None and refreshed.state is RunState.MONITORING_ACTIVE
    assert pull is not None and pull.merged is False and pull.merged_at is None


async def test_a_replayed_delivery_changes_nothing(client: AsyncClient) -> None:
    """C8-06 acceptance. GitHub retries, and a retry must be inert."""
    _, run, _ = await _open_pull_request()
    body = _payload(merged=True)

    first = await client.post("/webhooks/github", content=body, headers=_headers(body))
    second = await client.post("/webhooks/github", content=body, headers=_headers(body))

    assert first.json()["status"] == "merged"
    assert second.json()["status"] == "already_settled"

    async with session_scope() as session:
        from backend.models import StateTransition

        moves = list(
            (
                await session.execute(
                    select(StateTransition).where(
                        StateTransition.migration_run_id == run.id
                    )
                )
            ).scalars()
        )

    assert len([m for m in moves if m.to_state is RunState.VERIFIED]) == 1


async def test_a_verified_delivery_for_an_unknown_repository_does_nothing(
    client: AsyncClient,
) -> None:
    """Signed by GitHub, and still none of Continuity's business."""
    await _open_pull_request()
    body = json.dumps(
        {
            "action": "closed",
            "repository": {"full_name": "someone/else"},
            "pull_request": {"number": 7, "state": "closed", "merged": True},
        }
    ).encode()

    response = await client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.json()["status"] == "unknown_repository"


async def test_a_payload_cannot_invent_a_pull_request(client: AsyncClient) -> None:
    """The payload is a pointer, never state.

    A verified delivery naming a PR Continuity never opened must not create one,
    and must not advance anything.
    """
    _, run, _ = await _open_pull_request(number=7)
    body = _payload(number=999, merged=True)

    response = await client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.json()["status"] == "unknown_pull_request"

    async with session_scope() as session:
        count = len(list((await session.execute(select(PullRequest))).scalars()))
        refreshed = await session.get(MigrationRun, run.id)

    assert count == 1
    assert refreshed is not None and refreshed.state is RunState.MERGE_WAITING


async def test_an_open_pull_request_event_is_ignored(client: AsyncClient) -> None:
    """Only `closed` settles a run. An edit or a review does not."""
    _, run, _ = await _open_pull_request()
    body = _payload(state="open", merged=False)

    response = await client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.json()["status"] == "ignored"

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)
    assert refreshed is not None and refreshed.state is RunState.MERGE_WAITING


async def test_a_verified_delivery_of_another_event_is_ignored(client: AsyncClient) -> None:
    body = _payload()

    response = await client.post(
        "/webhooks/github", content=body, headers=_headers(body, event="push")
    )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


# --- webhooks not configured ---------------------------------------------


async def test_with_no_secret_every_delivery_is_refused(
    unconfigured_settings: Settings, tmp_path: Path
) -> None:
    """The deployment this project actually runs: webhooks intentionally off.

    Accepting unsigned deliveries here would let anyone who can reach the URL
    advance a migration run. Refusing means the run stays in `MERGE_WAITING`,
    which is the honest outcome.
    """
    from backend.models.session import dispose_engine, init_engine

    init_engine(unconfigured_settings)
    await create_all()
    try:
        app = create_app(unconfigured_settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            body = _payload()
            response = await http.post(
                "/webhooks/github", content=body, headers=_headers(body)
            )

            assert response.status_code == 401

        async with session_scope() as session:
            audit = (await session.execute(select(AuditEvent))).scalars().first()

        assert audit is not None
        assert audit.detail["reason"] == "no webhook secret configured"
    finally:
        await dispose_engine()


# --- the polling fallback -------------------------------------------------


class FakePolledGitHub:
    def __init__(self, payloads: dict[int, dict[str, Any]]) -> None:
        self.payloads = payloads
        self.asked: list[int] = []

    async def get_pull_request(self, owner: str, name: str, number: int) -> dict[str, Any]:
        self.asked.append(number)
        if number not in self.payloads:
            raise RuntimeError("not found")
        return self.payloads[number]


async def test_polling_settles_a_merged_pull_request(database: None) -> None:
    """C8-06 acceptance: the fallback for deployments with no inbound webhooks.

    Converges on the same `settle()`, so the state machine does not care which
    mechanism found out.
    """
    from backend.workers.pr_status_poll import poll_open_pull_requests

    record, run, _ = await _open_pull_request()
    client = FakePolledGitHub({record.number: {"state": "closed", "merged": True}})

    async with session_scope() as session:
        result = await poll_open_pull_requests(session, client=client)

    assert result.checked == 1
    assert result.settled == [f"#{record.number}: merged"]

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)
    assert refreshed is not None and refreshed.state is RunState.VERIFIED


async def test_polling_leaves_an_open_pull_request_alone(database: None) -> None:
    from backend.workers.pr_status_poll import poll_open_pull_requests

    record, run, _ = await _open_pull_request()
    client = FakePolledGitHub({record.number: {"state": "open", "merged": False}})

    async with session_scope() as session:
        result = await poll_open_pull_requests(session, client=client)

    assert result.settled == []

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)
    assert refreshed is not None and refreshed.state is RunState.MERGE_WAITING


async def test_one_unreachable_repository_does_not_stop_the_sweep(
    database: None,
) -> None:
    from backend.workers.pr_status_poll import poll_open_pull_requests

    await _open_pull_request(number=1, repo_name="one")
    _, second_run, _ = await _open_pull_request(number=2, repo_name="two")
    client = FakePolledGitHub({2: {"state": "closed", "merged": True}})

    async with session_scope() as session:
        result = await poll_open_pull_requests(session, client=client)

    assert result.checked == 2
    assert result.errors and "#1" in result.errors[0]
    assert result.settled == ["#2: merged"]

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, second_run.id)
    assert refreshed is not None and refreshed.state is RunState.VERIFIED


def test_only_post_merge_verification_can_mark_a_run_verified() -> None:
    """VERIFIED is earned, and one module grants it.

    This used to name `github/webhooks.py`, because a merge went straight to
    VERIFIED. It does not any more: the webhook and the poller both converge on
    `settle()`, `settle()` calls verification, and verification is the only
    thing that can move a run to VERIFIED and advance the baseline. Narrowing to
    one owner is what stops "merged" and "verified" from meaning the same thing.

    Matched against parsed code rather than raw text, and narrowed to modules
    that can actually *move* a run: `models/base.py` names the member in a
    docstring, and `workers/scheduler.py` reads it to decide which projects are
    idle enough to sweep. Neither writes it, and counting either as a writer
    would make this test fire on every honest change.

    The real property: a module can only take a run to VERIFIED if it both
    names the state and calls `transition`.
    """
    import ast

    backend = Path(__file__).resolve().parents[2] / "backend"
    setters = []

    for path in backend.rglob("*.py"):
        tree = ast.parse(path.read_text())

        names_verified = any(
            isinstance(node, ast.Attribute)
            and node.attr == "VERIFIED"
            # `state_machine.py` aliases the enum to `S` for readability.
            and isinstance(node.value, ast.Name)
            and node.value.id in {"RunState", "S"}
            for node in ast.walk(tree)
        )
        moves_runs = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "transition"
            for node in ast.walk(tree)
        )

        if names_verified and moves_runs:
            setters.append(path.relative_to(backend).as_posix())

    assert sorted(setters) == ["workers/post_merge.py"]


def test_the_state_machine_permits_exactly_two_routes_into_verified() -> None:
    """The other half: what the graph itself allows.

    `settle()` is the only code that moves a run there, and these are the only
    two edges that exist for it to use.
    """
    from backend.models.enums import RunState
    from backend.orchestration.state_machine import ALLOWED_TRANSITIONS

    sources = {
        state
        for state, targets in ALLOWED_TRANSITIONS.items()
        if RunState.VERIFIED in targets
    }

    assert sources == {
        RunState.MERGE_WAITING,
        RunState.POST_MERGE_VERIFICATION_PASSED,
    }
