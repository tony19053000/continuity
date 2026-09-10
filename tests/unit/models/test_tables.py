"""C1-03 acceptance: every §14 table exists and round-trips.

The approval tests are the important ones here. They assert the database-level
guarantee behind `03_SECURITY_ACCESS.md` §4: a resolved approval cannot exist
without a real user attached to it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.models import (
    Approval,
    ApprovalStatus,
    Base,
    ChangeEvent,
    ChangeType,
    Confidence,
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
    PolicyDecision,
    Project,
    Repository,
    RunState,
    Severity,
    User,
)
from backend.models.session import get_engine, session_scope

# Every table named in 02_ARCHITECTURE.md §14, plus the two the architecture
# describes in prose (jobs in §15, state_transitions in §8).
EXPECTED_TABLES = {
    "users",
    "github_installations",
    "repositories",
    "projects",
    "integrations",
    "graph_nodes",
    "graph_edges",
    "providers",
    "provider_specs",
    "provider_baselines",
    "change_events",
    "agent_runs",
    "agent_steps",
    "tool_invocations",
    "jobs",
    "state_transitions",
    "migration_runs",
    "migration_attempts",
    "test_results",
    "security_findings",
    "approvals",
    "audit_events",
    "pull_requests",
    "activity_events",
}


def test_metadata_defines_every_documented_table() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_activity_event_vocabulary_matches_the_architecture_document() -> None:
    """The §16 vocabulary is closed, and this is what keeps it closed.

    Parsed from the document rather than retyped, so the two cannot drift: if
    someone adds an event name to one and not the other, this fails.
    """
    import re
    from pathlib import Path

    from backend.models import ActivityEventKind

    architecture = (
        Path(__file__).resolve().parents[3] / "02_ARCHITECTURE.md"
    ).read_text()

    section = architecture.split("## 16. Observability", 1)[1].split("\n---", 1)[0]
    vocabulary_block = section.split("Structured activity events", 1)[1].split(
        "Each event carries", 1
    )[0]
    documented = set(re.findall(r"`([a-z_]+)`", vocabulary_block))

    assert documented == {member.value for member in ActivityEventKind}


async def test_schema_creates_every_table(database: None) -> None:
    from sqlalchemy import inspect

    engine = get_engine()
    async with engine.connect() as connection:
        names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    assert EXPECTED_TABLES <= names


async def _make_user(session, email: str = "dev@example.com") -> User:
    user = User(google_subject=f"sub-{uuid.uuid4()}", email=email)
    session.add(user)
    await session.flush()
    return user


async def _make_project(session, user: User) -> Project:
    repository = Repository(owner="acme", name=f"api-{uuid.uuid4().hex[:8]}")
    session.add(repository)
    await session.flush()

    project = Project(user_id=user.id, repository_id=repository.id, name="commerce-api")
    session.add(project)
    await session.flush()
    return project


async def test_user_round_trips(database: None) -> None:
    async with session_scope() as session:
        user = await _make_user(session, "someone@example.com")
        user_id = user.id

    async with session_scope() as session:
        loaded = await session.get(User, user_id)

    assert loaded is not None
    assert loaded.email == "someone@example.com"
    assert loaded.created_at is not None


async def test_project_defaults_to_project_created_state(database: None) -> None:
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)
        project_id = project.id

    async with session_scope() as session:
        loaded = await session.get(Project, project_id)

    assert loaded is not None
    assert loaded.state is RunState.PROJECT_CREATED


async def test_graph_nodes_and_edges_round_trip_with_evidence(database: None) -> None:
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)

        provider = GraphNode(
            project_id=project.id,
            graph_version=1,
            kind=NodeKind.PROVIDER,
            key="payments",
            label="Payment Provider",
            confidence=Confidence.CONFIRMED,
            evidence={"kind": "manifest", "file_path": "pyproject.toml", "line_start": 12},
        )
        symbol = GraphNode(
            project_id=project.id,
            graph_version=1,
            kind=NodeKind.SYMBOL,
            key="payment_service.create_payment",
            label="create_payment()",
            confidence=Confidence.CONFIRMED,
            evidence={"kind": "source", "file_path": "payment_service.py", "line_start": 40},
        )
        session.add_all([provider, symbol])
        await session.flush()

        session.add(
            GraphEdge(
                project_id=project.id,
                graph_version=1,
                kind=EdgeKind.CALLS_PROVIDER,
                source_node_id=symbol.id,
                target_node_id=provider.id,
                confidence=Confidence.CONFIRMED,
                evidence={"kind": "source", "file_path": "payment_service.py", "line_start": 44},
            )
        )
        project_id = project.id

    async with session_scope() as session:
        edges = (
            await session.execute(select(GraphEdge).where(GraphEdge.project_id == project_id))
        ).scalars().all()

    assert len(edges) == 1
    assert edges[0].kind is EdgeKind.CALLS_PROVIDER
    assert edges[0].evidence is not None


async def test_graph_versions_isolate_scans(database: None) -> None:
    """Two scans of the same file must coexist rather than collide."""
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)

        for graph_version in (1, 2):
            session.add(
                GraphNode(
                    project_id=project.id,
                    graph_version=graph_version,
                    kind=NodeKind.FILE,
                    key="payment_service.py",
                    label="payment_service.py",
                    confidence=Confidence.CONFIRMED,
                )
            )
        project_id = project.id

    async with session_scope() as session:
        nodes = (
            await session.execute(select(GraphNode).where(GraphNode.project_id == project_id))
        ).scalars().all()

    assert {node.graph_version for node in nodes} == {1, 2}


async def test_change_event_deduplication_constraint(database: None) -> None:
    """Polling the same provider twice must not create a second event."""

    def _event() -> ChangeEvent:
        return ChangeEvent(
            provider_id="payments",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.WEBHOOK_EVENT_CHANGED,
            resource="payment.paid",
            breaking=True,
            source={"kind": "changelog", "url": "https://example.test/changelog"},
            evidence={"kind": "changelog", "confidence": "confirmed"},
            detected_at=datetime.now(UTC),
        )

    async with session_scope() as session:
        session.add(_event())

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(_event())


async def test_migration_attempt_numbers_are_unique_per_run(database: None) -> None:
    """The retry budget is enforced by the database as well as by code."""
    from backend.models import MigrationAttempt, MigrationRun

    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)

        change = ChangeEvent(
            provider_id="payments",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.ENDPOINT_REMOVED,
            resource="/v1/charges",
            breaking=True,
            source={"kind": "openapi_spec"},
            evidence={"kind": "provider_spec", "confidence": "confirmed"},
            detected_at=datetime.now(UTC),
        )
        session.add(change)
        await session.flush()

        run = MigrationRun(
            project_id=project.id,
            change_event_id=change.id,
            provider_id="payments",
            from_version="v1",
            to_version="v2",
            state=RunState.MIGRATION_RUNNING,
        )
        session.add(run)
        await session.flush()

        session.add(MigrationAttempt(migration_run_id=run.id, attempt_number=1))
        run_id = run.id

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(MigrationAttempt(migration_run_id=run_id, attempt_number=1))


# --- Approval integrity --------------------------------------------------


async def test_a_resolved_approval_cannot_exist_without_an_actor(database: None) -> None:
    """The core security guarantee, enforced by the database.

    If this ever passes without raising, an agent-driven code path could write
    `status=APPROVED, actor_user_id=NULL` and manufacture its own authorization.
    """
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)
        project_id = project.id

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(
                Approval(
                    project_id=project_id,
                    trigger="oauth_scope_expansion",
                    risk=Severity.HIGH,
                    requested_action={},
                    status=ApprovalStatus.APPROVED,
                    actor_user_id=None,  # nobody approved this
                    resolved_at=datetime.now(UTC),
                )
            )


async def test_a_rejected_approval_also_requires_an_actor(database: None) -> None:
    """Rejection is a human decision too, and is equally auditable."""
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)
        project_id = project.id

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(
                Approval(
                    project_id=project_id,
                    trigger="oauth_scope_expansion",
                    risk=Severity.HIGH,
                    requested_action={},
                    status=ApprovalStatus.REJECTED,
                    actor_user_id=None,
                    resolved_at=datetime.now(UTC),
                )
            )


async def test_a_resolved_approval_must_record_when(database: None) -> None:
    """No holes in the audit trail: a decision has a timestamp."""
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)
        project_id, user_id = project.id, user.id

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(
                Approval(
                    project_id=project_id,
                    trigger="oauth_scope_expansion",
                    risk=Severity.HIGH,
                    requested_action={},
                    status=ApprovalStatus.APPROVED,
                    actor_user_id=user_id,
                    resolved_at=None,
                )
            )


async def test_approval_starts_pending_with_no_actor(database: None) -> None:
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)

        approval = Approval(
            project_id=project.id,
            trigger="oauth_scope_expansion",
            risk=Severity.HIGH,
            requested_action={"from": "customers.read", "to": "customers.write"},
        )
        session.add(approval)
        await session.flush()
        approval_id = approval.id

    async with session_scope() as session:
        loaded = await session.get(Approval, approval_id)

    assert loaded is not None
    assert loaded.status is ApprovalStatus.PENDING
    assert loaded.actor_user_id is None
    assert loaded.resolved_at is None


async def test_approval_records_the_human_who_granted_it(database: None) -> None:
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)

        approval = Approval(
            project_id=project.id,
            trigger="oauth_scope_expansion",
            risk=Severity.HIGH,
            requested_action={"from": "customers.read", "to": "customers.write"},
        )
        session.add(approval)
        await session.flush()

        approval.status = ApprovalStatus.APPROVED
        approval.actor_user_id = user.id
        approval.resolved_at = datetime.now(UTC)
        approval_id, user_id = approval.id, user.id

    async with session_scope() as session:
        loaded = await session.get(Approval, approval_id)

    assert loaded is not None
    assert loaded.status is ApprovalStatus.APPROVED
    assert loaded.actor_user_id == user_id


async def test_approval_actor_must_reference_a_real_user(database: None) -> None:
    """A fabricated user id cannot be attached to an approval.

    This is the database-level half of the approval guarantee: even code that
    bypasses the approval service cannot invent an approver.
    """
    async with session_scope() as session:
        user = await _make_user(session)
        project = await _make_project(session, user)
        project_id = project.id

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(
                Approval(
                    project_id=project_id,
                    trigger="oauth_scope_expansion",
                    risk=Severity.HIGH,
                    requested_action={},
                    status=ApprovalStatus.APPROVED,
                    actor_user_id=uuid.uuid4(),  # never inserted
                    resolved_at=datetime.now(UTC),
                )
            )


async def test_security_finding_stores_both_recommendation_and_decision(
    database: None,
) -> None:
    """Disagreement between the agent and the policy engine must be visible."""
    from backend.models import FindingCategory, SecurityFinding

    async with session_scope() as session:
        finding = SecurityFinding(
            category=FindingCategory.OAUTH_SCOPE_CHANGE,
            severity=Severity.HIGH,
            summary="Migration widens the customers scope.",
            evidence={"kind": "source", "confidence": "confirmed"},
            recommendation=PolicyDecision.ASK,
            policy_decision=PolicyDecision.DENY,
        )
        session.add(finding)
        await session.flush()
        finding_id = finding.id

    async with session_scope() as session:
        loaded = await session.get(SecurityFinding, finding_id)

    assert loaded is not None
    assert loaded.recommendation is PolicyDecision.ASK
    assert loaded.policy_decision is PolicyDecision.DENY
