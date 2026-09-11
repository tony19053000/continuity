"""C8-03: branch-and-PR delivery, and the three gates in front of it.

The GitHub API is a fake. Deliberately: opening a real pull request on every
test run is exactly the "unnecessary branches and PRs just for testing" the
project forbids, and what these tests are about is what Continuity *decides*,
not whether httpx works.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.approvals.service import ApprovalRequest, create_request, resolve
from backend.github.delivery import (
    BRANCH_PATTERN,
    DeliveryPreconditions,
    DeliveryRefused,
    branch_name,
    deliver,
)
from backend.models import (
    ChangeEvent,
    MigrationRun,
    Project,
    PullRequest,
    Repository,
    RunState,
    Severity,
    User,
)
from backend.models.enums import ChangeType, PolicyDecision
from backend.models.session import session_scope
from tests.support.delivery_fixtures import ExplodingGitHub, FakeGitHub

pytestmark = pytest.mark.usefixtures("database")

DEFAULT_BRANCH = "main"
FILES = {"app/client.py": "def charge(amount, currency='usd'): ...\n"}


async def _run(state: RunState = RunState.PR_PENDING) -> tuple[MigrationRun, Repository, User]:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="d@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"app-{uuid.uuid4().hex[:8]}")
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
            state=state,
        )
        session.add(run)
        await session.flush()
        await session.refresh(run)
        await session.refresh(repository)
        await session.refresh(user)
        return run, repository, user


def _ok(**overrides: Any) -> DeliveryPreconditions:
    return DeliveryPreconditions(
        validation_passed=overrides.pop("validation_passed", True),
        security_decision=overrides.pop("security_decision", PolicyDecision.ALLOW),
        required_approval_ids=overrides.pop("required_approval_ids", []),
    )


async def _deliver(run: MigrationRun, repository: Repository, **kwargs: Any):
    client = kwargs.pop("client", FakeGitHub())
    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        delivered = await deliver(
            session,
            tracked,
            repository,
            client=client,  # type: ignore[arg-type]
            preconditions=kwargs.pop("preconditions", _ok()),
            default_branch=kwargs.pop("default_branch", DEFAULT_BRANCH),
            files=kwargs.pop("files", FILES),
            title=kwargs.pop("title", "Migrate acmepay v1 to v2"),
            body=kwargs.pop("body", "## Continuity\n\nEvidence."),
            commit_message=kwargs.pop("commit_message", "migrate acmepay to v2"),
        )
    return delivered, client


# --- branch naming -------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "version", "expected"),
    [
        ("acmepay", "v2", "continuity/migrate-acmepay-v2"),
        ("AcmePay", "V2", "continuity/migrate-acmepay-v2"),
        ("acme pay", "2024-06-01", "continuity/migrate-acme-pay-2024-06-01"),
        ("acme/pay", "v2", "continuity/migrate-acme-pay-v2"),
    ],
)
def test_branch_names_match_the_documented_pattern(
    provider: str, version: str, expected: str
) -> None:
    """C8-03 acceptance."""
    name = branch_name(provider, version)

    assert name == expected
    assert BRANCH_PATTERN.match(name)


@pytest.mark.parametrize(
    ("provider", "version"),
    [("../../evil", "v2"), ("acmepay", "../main"), ("", "v2"), ("acmepay", "")],
)
def test_a_traversing_or_empty_name_is_refused(provider: str, version: str) -> None:
    """`refs/heads/../..` is a real thing to be careful about.

    Provider ids and version strings arrive from external documents, so they
    are slugged and then checked rather than trusted.
    """
    try:
        name = branch_name(provider, version)
    except DeliveryRefused:
        return
    assert BRANCH_PATTERN.match(name)
    assert ".." not in name


# --- the three gates -----------------------------------------------------


async def test_delivery_is_refused_when_validation_has_not_passed() -> None:
    """C8-03 acceptance, refusal one of three.

    Continuity does not open a pull request for a patch whose tests it has not
    seen succeed.
    """
    run, repository, _ = await _run()

    with pytest.raises(DeliveryRefused, match="validation has not passed"):
        await _deliver(run, repository, preconditions=_ok(validation_passed=False))

    await _assert_nothing_was_delivered(run)


@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.ASK])
async def test_delivery_is_refused_when_the_security_review_did_not_allow(
    decision: PolicyDecision,
) -> None:
    """C8-03 acceptance, refusal two of three.

    ASK refuses as firmly as DENY: an unanswered question is not permission.
    """
    run, repository, _ = await _run()

    with pytest.raises(DeliveryRefused, match="security review"):
        await _deliver(run, repository, preconditions=_ok(security_decision=decision))

    await _assert_nothing_was_delivered(run)


async def test_delivery_is_refused_while_an_approval_is_pending() -> None:
    """C8-03 acceptance, refusal three of three."""
    run, repository, _ = await _run()

    async with session_scope() as session:
        approval = await create_request(
            session,
            ApprovalRequest(
                project_id=run.project_id,
                migration_run_id=run.id,
                trigger="install_dependency",
                risk=Severity.MEDIUM,
                requested_action={"package": "left-pad"},
            ),
        )
        approval_id = approval.id

    with pytest.raises(DeliveryRefused, match="pending"):
        await _deliver(
            run, repository, preconditions=_ok(required_approval_ids=[approval_id])
        )

    await _assert_nothing_was_delivered(run)


async def test_delivery_proceeds_once_the_approval_is_granted() -> None:
    run, repository, user = await _run()

    async with session_scope() as session:
        approval = await create_request(
            session,
            ApprovalRequest(
                project_id=run.project_id,
                migration_run_id=run.id,
                trigger="install_dependency",
                risk=Severity.MEDIUM,
                requested_action={"package": "left-pad"},
            ),
        )
        approval_id = approval.id

    async with session_scope() as session:
        await resolve(session, approval_id, actor=user, approved=True)

    delivered, _ = await _deliver(
        run, repository, preconditions=_ok(required_approval_ids=[approval_id])
    )

    assert delivered.number == 42


async def test_an_approval_revoked_before_delivery_stops_it() -> None:
    """The gate is checked immediately before the write, not earlier in the run."""
    run, repository, user = await _run()

    async with session_scope() as session:
        approval = await create_request(
            session,
            ApprovalRequest(
                project_id=run.project_id,
                migration_run_id=run.id,
                trigger="install_dependency",
                risk=Severity.MEDIUM,
                requested_action={"package": "left-pad"},
            ),
        )
        approval_id = approval.id

    async with session_scope() as session:
        await resolve(session, approval_id, actor=user, approved=True)

    # The person changes their mind.
    async with session_scope() as session:
        from backend.models import Approval, ApprovalStatus

        revoked = await session.get(Approval, approval_id)
        assert revoked is not None
        revoked.status = ApprovalStatus.REJECTED
        await session.flush()

    with pytest.raises(DeliveryRefused):
        await _deliver(
            run, repository, preconditions=_ok(required_approval_ids=[approval_id])
        )

    await _assert_nothing_was_delivered(run)


async def test_the_default_branch_is_never_the_target() -> None:
    """C8-03 acceptance.

    A repository whose default branch happened to be named like one of ours
    would otherwise be written to directly.
    """
    run, repository, _ = await _run()

    with pytest.raises(DeliveryRefused, match="default branch"):
        await _deliver(run, repository, default_branch="continuity/migrate-acmepay-v2")

    await _assert_nothing_was_delivered(run)


async def test_an_empty_patch_is_not_delivered() -> None:
    """An empty pull request asks a reviewer to approve nothing."""
    run, repository, _ = await _run()

    with pytest.raises(DeliveryRefused, match="nothing to deliver"):
        await _deliver(run, repository, files={})

    await _assert_nothing_was_delivered(run)


async def test_a_refusal_writes_nothing_at_all() -> None:
    """A gate that refuses after creating a branch would leave litter behind."""
    run, repository, _ = await _run()
    client = FakeGitHub()

    with pytest.raises(DeliveryRefused):
        await _deliver(
            run, repository, client=client, preconditions=_ok(validation_passed=False)
        )

    assert client.called == []


# --- the happy path ------------------------------------------------------


async def test_delivery_creates_a_branch_commits_and_opens_a_pull_request() -> None:
    delivered, client = await _deliver(*(await _run())[:2])

    assert client.called == [
        "get_ref",
        "create_branch",
        "create_blob",
        "create_tree",
        "create_commit",
        "update_branch",
        "create_pull_request",
    ]
    assert delivered.branch == "continuity/migrate-acmepay-v2"
    assert delivered.number == 42
    assert delivered.files_changed == 1

    (_, pull) = next(c for c in client.calls if c[0] == "create_pull_request")
    assert pull["head"] == "continuity/migrate-acmepay-v2"
    assert pull["base"] == DEFAULT_BRANCH


async def test_the_pull_request_is_recorded() -> None:
    run, repository, _ = await _run()

    delivered, _ = await _deliver(run, repository)

    async with session_scope() as session:
        row = (
            await session.execute(
                select(PullRequest).where(PullRequest.migration_run_id == run.id)
            )
        ).scalar_one()

    assert row.number == delivered.number
    assert row.branch == delivered.branch
    assert row.state == "open"
    assert row.merged is False


async def test_the_run_reaches_merge_waiting_by_a_legal_path() -> None:
    """PR_PENDING → PR_CREATING → PR_CREATED → MERGE_WAITING.

    Each step means something different, and `ALLOWED_TRANSITIONS` has no edge
    that skips one.
    """
    from backend.models import StateTransition
    from backend.orchestration.state_machine import can_transition

    run, repository, _ = await _run()

    await _deliver(run, repository)

    async with session_scope() as session:
        steps = list(
            (
                await session.execute(
                    select(StateTransition)
                    .where(StateTransition.migration_run_id == run.id)
                    .order_by(StateTransition.created_at, StateTransition.id)
                )
            ).scalars()
        )
        refreshed = await session.get(MigrationRun, run.id)

    assert [s.to_state for s in steps] == [
        RunState.PR_CREATING,
        RunState.PR_CREATED,
        RunState.MERGE_WAITING,
    ]
    for step in steps:
        assert can_transition(step.from_state, step.to_state)
    assert refreshed is not None
    assert refreshed.state is RunState.MERGE_WAITING
    assert refreshed.target_branch == "continuity/migrate-acmepay-v2"


async def test_a_failure_during_delivery_records_nothing() -> None:
    """The unit of work rolls back, so the run is exactly where it was.

    That is the right outcome here: the GitHub call that failed is the *first*
    write, so nothing was created upstream either and the two stay consistent.

    A failure later in the sequence — branch created, pull request call fails —
    is a different case: GitHub keeps the branch while the transaction rolls
    back, and the next attempt finds the ref already present. Continuity does
    not reuse it, because a branch that already exists may carry someone else's
    commits. It is recorded as a known gap rather than silently handled.
    """
    run, repository, _ = await _run()

    with pytest.raises(RuntimeError, match="unreachable"):
        await _deliver(run, repository, client=ExplodingGitHub())

    async with session_scope() as session:
        refreshed = await session.get(MigrationRun, run.id)

    assert refreshed is not None
    assert refreshed.state is RunState.PR_PENDING
    await _assert_nothing_was_delivered(run)


async def test_the_pull_request_body_is_secret_filtered() -> None:
    """The last stop before the text becomes visible to everyone with repo access."""
    from tests.support.secret_samples import GITHUB_TOKEN

    run, repository, _ = await _run()

    _, client = await _deliver(
        run,
        repository,
        body=f"## Evidence\n\nThe patch set TOKEN={GITHUB_TOKEN}\n",
        title=f"Migrate with {GITHUB_TOKEN}",
        commit_message=f"migrate {GITHUB_TOKEN}",
    )

    (_, pull) = next(c for c in client.calls if c[0] == "create_pull_request")
    (_, commit) = next(c for c in client.calls if c[0] == "create_commit")

    assert GITHUB_TOKEN not in pull["body"]
    assert GITHUB_TOKEN not in pull["title"]
    assert GITHUB_TOKEN not in commit["message"]


# --- no dangerous operations exist ---------------------------------------


def test_the_github_client_has_no_dangerous_write() -> None:
    """Branch-and-PR only, enforced by absence rather than by refusal.

    There is no force-push method to call, no merge method, and no way to write
    to an arbitrary ref. A caller cannot misuse what does not exist.
    """
    from backend.github.client import GitHubAppClient

    surface = {name for name in dir(GitHubAppClient) if not name.startswith("__")}

    for forbidden in ("force_push", "merge", "merge_pull_request", "delete_ref",
                      "delete_branch", "delete_repository", "rewrite", "push"):
        assert forbidden not in surface, f"GitHubAppClient exposes {forbidden}"


def test_no_force_parameter_is_ever_sent() -> None:
    """`update_branch` moves one of our own branches, and only fast-forward.

    Sending `force: true` would let a failed migration overwrite whatever was
    on the branch, including a reviewer's own commit.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "backend" / "github" / "client.py"
    ).read_text()

    assert '"force"' not in source
    assert "force=True" not in source


async def _assert_nothing_was_delivered(run: MigrationRun) -> None:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(func.count())
                .select_from(PullRequest)
                .where(PullRequest.migration_run_id == run.id)
            )
        ).scalar_one()
    assert rows == 0
