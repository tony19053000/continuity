"""SQLAlchemy table definitions for every entity in `02_ARCHITECTURE.md` §14.

Design notes worth knowing before editing:

* **A resolved approval must name an existing user and a resolution time.**
  `approvals.actor_user_id` is foreign-keyed to `users`, and CHECK constraints
  require both it and `resolved_at` on any row whose status is APPROVED or
  REJECTED. That is the schema's half of `03_SECURITY_ACCESS.md` §4: it makes an
  unattributed approval impossible to store. Binding that user to the
  *authenticated human who actually clicked approve* is the approval service's
  job (C8-02) — the database can prove attribution, not consent.
* **The graph is versioned, not mutated.** Each scan writes a new
  `graph_version`; queries read the latest. History is therefore free, and a
  scan can never corrupt the graph a running migration is reading.
* **Evidence travels with the row.** Anything asserting a fact about code or a
  provider carries an `evidence` JSON column, so no claim in the UI is
  unsourced.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base, JsonDict, Timestamps, UUIDPrimaryKey
from backend.models.enums import (
    ActivityEventKind,
    AgentRole,
    ApprovalStatus,
    AttemptOutcome,
    ChangeType,
    Confidence,
    EdgeKind,
    FindingCategory,
    JobKind,
    JobStatus,
    NodeKind,
    PolicyDecision,
    RunState,
    Severity,
)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class User(UUIDPrimaryKey, Timestamps, Base):
    """A Continuity user.

    Authentication is delegated to Google; no password is ever stored.
    `google_subject` is Google's stable subject id, which does not change when a
    user changes their email address.
    """

    __tablename__ = "users"

    google_subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(1024))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped on sign-out, which invalidates every session token issued before
    # it. Without this, session cookies would be unrevocable bearer tokens for
    # their full lifetime and "sign out" would only clear the local browser.
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class GitHubInstallation(UUIDPrimaryKey, Timestamps, Base):
    """A GitHub App installation and the repositories it authorized.

    Holds no token: installation tokens are minted per operation, kept in
    memory, and never persisted (`03_SECURITY_ACCESS.md` §3).
    """

    __tablename__ = "github_installations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    installation_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    account_login: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(32))
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


# ---------------------------------------------------------------------------
# Projects and repositories
# ---------------------------------------------------------------------------


class Repository(UUIDPrimaryKey, Timestamps, Base):
    """A repository Continuity has been authorized to read."""

    __tablename__ = "repositories"

    installation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("github_installations.id"), index=True
    )
    github_repo_id: Mapped[int | None] = mapped_column(Integer, index=True)
    owner: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    # Set for the local development adapter only; never for GitHub sources.
    local_path: Mapped[str | None] = mapped_column(String(4096))

    __table_args__ = (UniqueConstraint("owner", "name", name="uq_repository_owner_name"),)


class Project(UUIDPrimaryKey, Timestamps, Base):
    """A monitored project: one repository plus its integration state."""

    __tablename__ = "projects"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    state: Mapped[RunState] = mapped_column(default=RunState.PROJECT_CREATED, index=True)
    current_graph_version: Mapped[int | None] = mapped_column(Integer)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped[Repository] = relationship()


class Integration(UUIDPrimaryKey, Timestamps, Base):
    """A provider used by a project — the summary view over the graph."""

    __tablename__ = "integrations"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(128), index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    sdk_package: Mapped[str | None] = mapped_column(String(255))
    detected_api_version: Mapped[str | None] = mapped_column(String(64))
    auth_mechanism: Mapped[str | None] = mapped_column(String(64))
    # How many call sites reach this provider. Surfaced on the Integrations
    # page ("23 Integration Points") and recomputed on every scan.
    integration_points: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confidence: Mapped[Confidence] = mapped_column(default=Confidence.INFERRED)
    evidence: Mapped[JsonDict | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("project_id", "provider_id", name="uq_integration_project_provider"),
    )


# ---------------------------------------------------------------------------
# Integration Intelligence Graph
# ---------------------------------------------------------------------------


class GraphNode(UUIDPrimaryKey, Timestamps, Base):
    """A node in the Integration Intelligence Graph.

    `key` is a stable, kind-scoped identifier (a file path, a qualified symbol
    name, a provider id) so the same real-world entity keeps its identity across
    graph versions and can be diffed between scans.
    """

    __tablename__ = "graph_nodes"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    graph_version: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[NodeKind] = mapped_column(index=True)
    key: Mapped[str] = mapped_column(String(1024))
    label: Mapped[str] = mapped_column(String(512))
    attributes: Mapped[JsonDict | None] = mapped_column()
    confidence: Mapped[Confidence] = mapped_column()
    evidence: Mapped[JsonDict | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint(
            "project_id", "graph_version", "kind", "key", name="uq_graph_node_identity"
        ),
        Index("ix_graph_node_lookup", "project_id", "graph_version", "kind"),
    )


class GraphEdge(UUIDPrimaryKey, Timestamps, Base):
    """A typed, evidenced relationship between two graph nodes."""

    __tablename__ = "graph_edges"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    graph_version: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[EdgeKind] = mapped_column(index=True)
    source_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("graph_nodes.id"), index=True)
    target_node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("graph_nodes.id"), index=True)
    attributes: Mapped[JsonDict | None] = mapped_column()
    confidence: Mapped[Confidence] = mapped_column()
    evidence: Mapped[JsonDict | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint(
            "graph_version", "kind", "source_node_id", "target_node_id", name="uq_graph_edge"
        ),
        Index("ix_graph_edge_traverse", "project_id", "graph_version", "source_node_id"),
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class Provider(UUIDPrimaryKey, Timestamps, Base):
    """A monitored external provider, as registered by its adapter."""

    __tablename__ = "providers"

    provider_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    adapter_name: Mapped[str] = mapped_column(String(128))
    capabilities: Mapped[JsonDict | None] = mapped_column()
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_error: Mapped[str | None] = mapped_column(Text)


class ProviderSpec(UUIDPrimaryKey, Timestamps, Base):
    """A fetched provider document, content-addressed and attributable.

    `content_hash` is unique, so refetching identical content stores one row and
    change detection never fires on a no-op refetch.
    """

    __tablename__ = "provider_specs"

    provider_id: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[JsonDict] = mapped_column()
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    parse_error: Mapped[str | None] = mapped_column(Text)


class ProviderBaseline(UUIDPrimaryKey, Timestamps, Base):
    """The provider version a project is currently known to work against."""

    __tablename__ = "provider_baselines"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(64))
    spec_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("provider_specs.id"))
    established_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[JsonDict | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("project_id", "provider_id", name="uq_baseline_project_provider"),
    )


class ChangeEvent(UUIDPrimaryKey, Timestamps, Base):
    """A detected provider change.

    The unique constraint is the deduplication key from `02_ARCHITECTURE.md`
    §10: polling the same version twice cannot create a second event.
    """

    __tablename__ = "change_events"

    provider_id: Mapped[str] = mapped_column(String(128), index=True)
    old_version: Mapped[str] = mapped_column(String(64))
    new_version: Mapped[str] = mapped_column(String(64))
    change_type: Mapped[ChangeType] = mapped_column(index=True)
    resource: Mapped[str] = mapped_column(String(1024))
    old_contract: Mapped[JsonDict | None] = mapped_column()
    new_contract: Mapped[JsonDict | None] = mapped_column()
    breaking: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    security_relevant: Mapped[bool] = mapped_column(Boolean, default=False)
    authentication_relevant: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[JsonDict] = mapped_column()
    evidence: Mapped[JsonDict] = mapped_column()
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "old_version",
            "new_version",
            "change_type",
            "resource",
            name="uq_change_event_dedup",
        ),
    )


# ---------------------------------------------------------------------------
# Runs, agents, jobs
# ---------------------------------------------------------------------------


class AgentRun(UUIDPrimaryKey, Timestamps, Base):
    """One invocation of a runtime agent within a workflow run."""

    __tablename__ = "agent_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("migration_runs.id"), index=True
    )
    role: Mapped[AgentRole] = mapped_column(index=True)
    state: Mapped[RunState] = mapped_column(index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    token_usage: Mapped[JsonDict | None] = mapped_column()


class AgentStep(UUIDPrimaryKey, Timestamps, Base):
    """A concise, user-facing step within an agent run.

    Deliberately a *summary*, not a transcript: raw model chain-of-thought is
    never persisted (`02_ARCHITECTURE.md` §16).
    """

    __tablename__ = "agent_steps"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("agent_run_id", "sequence", name="uq_agent_step_seq"),)


class ToolInvocation(UUIDPrimaryKey, Timestamps, Base):
    """A tool call, its policy decision, and its outcome.

    Every dispatch writes one of these — including refusals — so
    `unauthorized_action_attempt` is a query, not a guess.
    """

    __tablename__ = "tool_invocations"

    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    arguments: Mapped[JsonDict | None] = mapped_column()
    policy_decision: Mapped[PolicyDecision] = mapped_column(index=True)
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    result_summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class Job(UUIDPrimaryKey, Timestamps, Base):
    """A background job.

    Database-backed so queued work survives a restart, and so the in-process
    runner can later be swapped for SQS or Step Functions without callers
    noticing (`02_ARCHITECTURE.md` §15).
    """

    __tablename__ = "jobs"

    kind: Mapped[JobKind] = mapped_column(index=True)
    status: Mapped[JobStatus] = mapped_column(default=JobStatus.QUEUED, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    payload: Mapped[JsonDict | None] = mapped_column()
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class StateTransition(UUIDPrimaryKey, Timestamps, Base):
    """An audited state change.

    The run timeline in the UI reads this table directly, so what the user sees
    is the recorded history rather than a narrative assembled after the fact.
    """

    __tablename__ = "state_transitions"

    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("migration_runs.id"), index=True
    )
    from_state: Mapped[RunState] = mapped_column()
    to_state: Mapped[RunState] = mapped_column()
    actor: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[JsonDict] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class MigrationRun(UUIDPrimaryKey, Timestamps, Base):
    """One end-to-end attempt to migrate a project past a provider change."""

    __tablename__ = "migration_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    change_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("change_events.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(128), index=True)
    from_version: Mapped[str] = mapped_column(String(64))
    to_version: Mapped[str] = mapped_column(String(64))
    state: Mapped[RunState] = mapped_column(index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255))
    source_commit: Mapped[str | None] = mapped_column(String(64))
    target_branch: Mapped[str | None] = mapped_column(String(255))
    rehearsal: Mapped[JsonDict | None] = mapped_column()
    evidence_report: Mapped[JsonDict | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MigrationAttempt(UUIDPrimaryKey, Timestamps, Base):
    """One patch-and-validate cycle inside the bounded repair loop.

    The unique constraint on `(migration_run_id, attempt_number)` means the
    retry budget is enforced by the database as well as by application code —
    the loop cannot exceed it even under a race.
    """

    __tablename__ = "migration_attempts"

    migration_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("migration_runs.id"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    plan_summary: Mapped[str | None] = mapped_column(Text)
    files_changed: Mapped[JsonDict | None] = mapped_column()
    patch_diff: Mapped[str | None] = mapped_column(Text)
    commands_executed: Mapped[JsonDict | None] = mapped_column()
    tests_executed: Mapped[JsonDict | None] = mapped_column()
    failure_evidence: Mapped[JsonDict | None] = mapped_column()
    diagnosis_summary: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[AttemptOutcome | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint("migration_run_id", "attempt_number", name="uq_attempt_number"),
    )


class TestResult(UUIDPrimaryKey, Timestamps, Base):
    """Parsed test execution output.

    Every field here comes from a real process. Nothing in this table may be
    produced by a model (`02_ARCHITECTURE.md` §13).
    """

    __tablename__ = "test_results"

    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("migration_runs.id"), index=True
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("migration_attempts.id"))
    suite: Mapped[str] = mapped_column(String(128))
    command: Mapped[str] = mapped_column(Text)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    failing_test_ids: Mapped[JsonDict | None] = mapped_column()
    raw_output_excerpt: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Security and approval
# ---------------------------------------------------------------------------


class SecurityFinding(UUIDPrimaryKey, Timestamps, Base):
    """A security finding.

    `recommendation` is the Security Reviewer agent's advice.
    `policy_decision` is the deterministic policy engine's ruling. Both are
    stored so a persistent disagreement between them is visible and
    investigable (`03_SECURITY_ACCESS.md` §10).
    """

    __tablename__ = "security_findings"

    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("migration_runs.id"), index=True
    )
    #: Which attempt this finding is about. A run makes several patches, and a
    #: finding against a patch that was discarded must not govern the one that
    #: ships — the rows stay as evidence, and the delivery gate reads only the
    #: attempt it is about to deliver. `None` means the finding is about the run
    #: rather than about one patch.
    attempt_number: Mapped[int | None] = mapped_column(default=None)
    category: Mapped[FindingCategory] = mapped_column(index=True)
    severity: Mapped[Severity] = mapped_column(index=True)
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[JsonDict] = mapped_column()
    recommendation: Mapped[PolicyDecision] = mapped_column()
    policy_decision: Mapped[PolicyDecision] = mapped_column()


class Approval(UUIDPrimaryKey, Timestamps, Base):
    """A human approval decision.

    A request starts `PENDING` with no actor, so `actor_user_id` cannot simply
    be NOT NULL. Two CHECK constraints enforce what the schema *can* enforce:

        a row whose status is APPROVED or REJECTED must reference an existing
        user and carry a resolution timestamp.

    That rules out an unattributed or untimed approval at the storage layer, so
    no code path can quietly leave one behind. It does **not** by itself prove a
    human consented — a caller could pass any valid user id. Binding
    `actor_user_id` to the authenticated approver is the approval service's
    responsibility (C8-02, `03_SECURITY_ACCESS.md` §4), and the constraints here
    are what stop that service from being bypassed silently rather than loudly.
    """

    __tablename__ = "approvals"

    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("migration_runs.id"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(128), index=True)
    risk: Mapped[Severity] = mapped_column()
    status: Mapped[ApprovalStatus] = mapped_column(default=ApprovalStatus.PENDING, index=True)
    requested_action: Mapped[JsonDict] = mapped_column()
    agent_recommendation: Mapped[str | None] = mapped_column(Text)
    # Null only while PENDING; required once resolved — see the CHECK below.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status = 'pending' OR actor_user_id IS NOT NULL",
            name="ck_approval_resolved_requires_actor",
        ),
        CheckConstraint(
            "status = 'pending' OR resolved_at IS NOT NULL",
            name="ck_approval_resolved_requires_timestamp",
        ),
    )


class AuditEvent(UUIDPrimaryKey, Timestamps, Base):
    """Security-relevant events, including every refused action.

    Separate from `activity_events` because the audience differs: activity is
    for the developer watching a run, audit is for answering "what did this
    system try to do, and what stopped it".
    """

    __tablename__ = "audit_events"

    kind: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    detail: Mapped[JsonDict] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ---------------------------------------------------------------------------
# Delivery and activity
# ---------------------------------------------------------------------------


class PullRequest(UUIDPrimaryKey, Timestamps, Base):
    """A pull request Continuity opened."""

    __tablename__ = "pull_requests"

    migration_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("migration_runs.id"), index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    number: Mapped[int] = mapped_column(Integer, index=True)
    url: Mapped[str] = mapped_column(String(1024))
    branch: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(32), default="open", index=True)
    merged: Mapped[bool] = mapped_column(Boolean, default=False)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    files_changed: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("repository_id", "number", name="uq_pull_request_repo_number"),
    )


class ActivityEvent(UUIDPrimaryKey, Timestamps, Base):
    """A user-facing activity event.

    `kind` is drawn from the fixed vocabulary in `02_ARCHITECTURE.md` §16;
    `summary` is concise and safe to display. No chain-of-thought, ever.
    """

    __tablename__ = "activity_events"

    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    migration_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("migration_runs.id"))
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[ActivityEventKind] = mapped_column(index=True)
    actor: Mapped[str] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[JsonDict | None] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
