"""The read surface the UI renders.

Everything here is a projection of stored records. No endpoint computes a status
from anything but rows, and none of them calls a model — which is what lets
`04_FRONTEND_SPEC.md`'s rule hold: no component contains a hardcoded provider,
change, workflow, test count, or status, because the API has none to give it.

Two habits carried from the rest of the codebase:

* **Ownership runs through the project.** A user who knows someone else's
  project id gets the same 404 as for one that does not exist.
* **Absent is absent.** A health score with no inputs is reported as
  unavailable rather than as a number, and a field with no record is `null`
  rather than a plausible default.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import current_user, get_db_session
from backend.api.health_score import IntegrationHealth, integration_health
from backend.migrations.evidence import build_report
from backend.models import (
    ActivityEvent,
    Approval,
    ChangeEvent,
    GraphEdge,
    GraphNode,
    Integration,
    MigrationRun,
    Project,
    Provider,
    PullRequest,
    Repository,
    RunState,
    SecurityFinding,
    User,
)
from backend.models.enums import ApprovalStatus, Confidence
from backend.security.findings import (
    attempt_scope,
    is_superseded,
    latest_attempt_numbers,
)
from backend.shared.errors import NotFound
from backend.shared.redaction import redact

router = APIRouter(prefix="/projects", tags=["projects"])

#: How many rows a list endpoint returns. The UI paginates; the API does not
#: hand a browser an unbounded result set.
PAGE = 100


class ProjectSummary(BaseModel):
    id: uuid.UUID
    name: str
    state: RunState
    repository: str | None
    providers: int
    integration_points: int
    open_changes: int
    pending_approvals: int
    health: dict[str, Any]


class IntegrationView(BaseModel):
    provider_id: str
    display_name: str
    sdk_package: str | None
    detected_api_version: str | None
    auth_mechanism: str | None
    integration_points: int
    confidence: Confidence
    last_checked_at: datetime | None
    last_check_error: str | None


class ChangeView(BaseModel):
    id: uuid.UUID
    provider_id: str
    old_version: str
    new_version: str
    change_type: str
    resource: str
    breaking: bool
    security_relevant: bool
    detected_at: datetime
    migration_run_id: uuid.UUID | None


class RunView(BaseModel):
    id: uuid.UUID
    provider_id: str
    from_version: str
    to_version: str
    state: RunState
    target_branch: str | None
    attempts: int
    findings: int
    pull_request: int | None
    created_at: datetime


class ActivityView(BaseModel):
    id: uuid.UUID
    kind: str
    actor: str
    summary: str
    occurred_at: datetime
    migration_run_id: uuid.UUID | None


class GraphView(BaseModel):
    """The Integration Intelligence Graph, as the UI draws it.

    `confidence` travels with every node and edge so the frontend can mark
    inferred data visibly — a rule the UI cannot honour if the API drops it.
    """

    version: int | None
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


async def _owned(session: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise NotFound("That project does not exist.")
    return project


@router.get("", response_model=list[ProjectSummary])
async def list_projects(
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ProjectSummary]:
    projects = list(
        (
            await session.execute(
                select(Project)
                .where(Project.user_id == user.id)
                .order_by(Project.created_at.desc())
            )
        ).scalars()
    )
    return [await _summarise(session, project) for project in projects]


@router.get("/{project_id}", response_model=ProjectSummary)
async def get_project(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProjectSummary:
    return await _summarise(session, await _owned(session, project_id, user))


@router.get("/{project_id}/integrations", response_model=list[IntegrationView])
async def list_integrations(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[IntegrationView]:
    project = await _owned(session, project_id, user)

    rows = list(
        (
            await session.execute(
                select(Integration)
                .where(Integration.project_id == project.id)
                .order_by(Integration.provider_id)
            )
        ).scalars()
    )

    views = []
    for row in rows:
        provider = (
            await session.execute(
                select(Provider).where(Provider.provider_id == row.provider_id)
            )
        ).scalar_one_or_none()
        views.append(
            IntegrationView(
                provider_id=row.provider_id,
                display_name=row.display_name,
                sdk_package=row.sdk_package,
                detected_api_version=row.detected_api_version,
                auth_mechanism=row.auth_mechanism,
                integration_points=row.integration_points,
                confidence=row.confidence,
                # Null, not "never" — a provider Continuity has not yet checked
                # is different from one it checked and found nothing wrong with.
                last_checked_at=provider.last_checked_at if provider else None,
                last_check_error=provider.last_check_error if provider else None,
            )
        )
    return views


@router.get("/{project_id}/changes", response_model=list[ChangeView])
async def list_changes(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ChangeView]:
    project = await _owned(session, project_id, user)

    providers = set(
        (
            await session.execute(
                select(Integration.provider_id).where(
                    Integration.project_id == project.id
                )
            )
        ).scalars()
    )
    if not providers:
        return []

    rows = list(
        (
            await session.execute(
                select(ChangeEvent)
                .where(ChangeEvent.provider_id.in_(providers))
                .order_by(ChangeEvent.detected_at.desc())
                .limit(PAGE)
            )
        ).scalars()
    )

    runs = {
        run.change_event_id: run.id
        for run in (
            await session.execute(
                select(MigrationRun).where(MigrationRun.project_id == project.id)
            )
        ).scalars()
    }

    return [
        ChangeView(
            id=row.id,
            provider_id=row.provider_id,
            old_version=row.old_version,
            new_version=row.new_version,
            change_type=row.change_type.value,
            resource=row.resource,
            breaking=row.breaking,
            security_relevant=row.security_relevant,
            detected_at=row.detected_at,
            migration_run_id=runs.get(row.id),
        )
        for row in rows
    ]


@router.get("/{project_id}/runs", response_model=list[RunView])
async def list_runs(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[RunView]:
    from backend.models import MigrationAttempt

    project = await _owned(session, project_id, user)

    rows = list(
        (
            await session.execute(
                select(MigrationRun)
                .where(MigrationRun.project_id == project.id)
                .order_by(MigrationRun.created_at.desc())
                .limit(PAGE)
            )
        ).scalars()
    )

    views = []
    for row in rows:
        attempts = (
            await session.execute(
                select(func.count())
                .select_from(MigrationAttempt)
                .where(MigrationAttempt.migration_run_id == row.id)
            )
        ).scalar_one()
        # Findings about the patch as it now stands. An earlier attempt that the
        # Red Team rejected leaves real findings behind, and counting them here
        # would describe this run's current patch by a patch that no longer
        # exists. They are still readable — and labelled — at `/findings`.
        latest = await latest_attempt_numbers(session, [row.id])
        findings = (
            await session.execute(
                select(func.count())
                .select_from(SecurityFinding)
                .where(
                    SecurityFinding.migration_run_id == row.id,
                    attempt_scope(latest.get(row.id)),
                )
            )
        ).scalar_one()
        pull = (
            await session.execute(
                select(PullRequest.number).where(
                    PullRequest.migration_run_id == row.id
                )
            )
        ).scalar_one_or_none()

        views.append(
            RunView(
                id=row.id,
                provider_id=row.provider_id,
                from_version=row.from_version,
                to_version=row.to_version,
                state=row.state,
                target_branch=row.target_branch,
                attempts=attempts,
                findings=findings,
                pull_request=pull,
                created_at=row.created_at,
            )
        )
    return views


@router.get("/{project_id}/runs/{run_id}/report")
async def get_report(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, Any]:
    """The evidence report — the same one that becomes the pull request body."""
    project = await _owned(session, project_id, user)
    run = await session.get(MigrationRun, run_id)
    if run is None or run.project_id != project.id:
        raise NotFound("That migration run does not exist.")

    report = await build_report(session, run)
    return {"report": report.as_dict(), "markdown": report.as_markdown()}


@router.get("/{project_id}/activity", response_model=list[ActivityView])
async def list_activity(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ActivityView]:
    """The run timeline, read straight from `activity_events`."""
    project = await _owned(session, project_id, user)

    rows = list(
        (
            await session.execute(
                select(ActivityEvent)
                .where(ActivityEvent.project_id == project.id)
                .order_by(ActivityEvent.occurred_at.desc())
                .limit(PAGE)
            )
        ).scalars()
    )
    return [
        ActivityView(
            id=row.id,
            kind=row.kind.value,
            actor=row.actor,
            summary=redact(row.summary),
            occurred_at=row.occurred_at,
            migration_run_id=row.migration_run_id,
        )
        for row in rows
    ]


@router.get("/{project_id}/graph", response_model=GraphView)
async def get_graph(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> GraphView:
    project = await _owned(session, project_id, user)

    version = (
        await session.execute(
            select(func.max(GraphNode.graph_version)).where(
                GraphNode.project_id == project.id
            )
        )
    ).scalar_one_or_none()

    if version is None:
        return GraphView(version=None, nodes=[], edges=[])

    nodes = list(
        (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.project_id == project.id,
                    GraphNode.graph_version == version,
                )
            )
        ).scalars()
    )
    edges = list(
        (
            await session.execute(
                select(GraphEdge).where(
                    GraphEdge.project_id == project.id,
                    GraphEdge.graph_version == version,
                )
            )
        ).scalars()
    )

    return GraphView(
        version=version,
        nodes=[
            {
                "id": str(node.id),
                "kind": node.kind.value,
                "key": node.key,
                "label": node.label,
                # Travels with every node so the UI can mark inferred data.
                "confidence": node.confidence.value,
            }
            for node in nodes
        ],
        edges=[
            {
                "id": str(edge.id),
                "kind": edge.kind.value,
                "source": str(edge.source_node_id),
                "target": str(edge.target_node_id),
                "confidence": edge.confidence.value,
            }
            for edge in edges
        ],
    )


@router.get("/{project_id}/findings")
async def list_findings(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[dict[str, Any]]:
    """Security findings, with both verdicts and whether they disagreed."""
    project = await _owned(session, project_id, user)

    rows = list(
        (
            await session.execute(
                select(SecurityFinding)
                .join(MigrationRun, MigrationRun.id == SecurityFinding.migration_run_id)
                .where(MigrationRun.project_id == project.id)
                .order_by(SecurityFinding.created_at.desc())
                .limit(PAGE)
            )
        ).scalars()
    )
    # Superseded findings are shown, not filtered away — an earlier patch
    # rejected for a reason is worth seeing — but they are labelled, so a
    # reader never mistakes history for a defect in the code about to ship.
    latest = await latest_attempt_numbers(
        session, [row.migration_run_id for row in rows if row.migration_run_id]
    )
    return [
        {
            "id": str(row.id),
            "migration_run_id": str(row.migration_run_id),
            "attempt_number": row.attempt_number,
            "superseded": is_superseded(row, latest),
            "category": row.category.value,
            "severity": row.severity.value,
            # Filtered on the way out, not only on the way in. The reviewer
            # redacts what it writes, but this endpoint is the last stop before
            # a browser and must not depend on every writer having remembered.
            "summary": redact(row.summary),
            "recommendation": row.recommendation.value,
            "policy_decision": row.policy_decision.value,
            "disagreed": row.recommendation is not row.policy_decision,
        }
        for row in rows
    ]


async def _summarise(session: AsyncSession, project: Project) -> ProjectSummary:
    repository = await session.get(Repository, project.repository_id)

    providers = (
        await session.execute(
            select(func.count())
            .select_from(Integration)
            .where(Integration.project_id == project.id)
        )
    ).scalar_one()

    points = (
        await session.execute(
            select(func.coalesce(func.sum(Integration.integration_points), 0)).where(
                Integration.project_id == project.id
            )
        )
    ).scalar_one()

    open_changes = (
        await session.execute(
            select(func.count())
            .select_from(MigrationRun)
            .where(
                MigrationRun.project_id == project.id,
                MigrationRun.state.notin_(
                    [RunState.VERIFIED, RunState.RUN_FAILED, RunState.MONITORING_ACTIVE]
                ),
            )
        )
    ).scalar_one()

    approvals = (
        await session.execute(
            select(func.count())
            .select_from(Approval)
            .where(
                Approval.project_id == project.id,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    ).scalar_one()

    health: IntegrationHealth = await integration_health(session, project)

    return ProjectSummary(
        id=project.id,
        name=project.name,
        state=project.state,
        repository=f"{repository.owner}/{repository.name}" if repository else None,
        providers=providers,
        integration_points=points,
        open_changes=open_changes,
        pending_approvals=approvals,
        health=health.summary(),
    )
