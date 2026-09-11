"""Connecting a repository, and getting it to the point of being monitored.

The entry point the product did not have. Everything downstream — the graph,
the baseline, the scheduler, the pipeline — depended on state that no production
code created, so a real user could not start. These are the two calls that
change that:

* `POST /repositories/import` — select an authorized repository and create the
  project.
* `POST /projects/{id}/scan` — scan, extract, map, and baseline it, leaving it
  monitorable.

The GitHub App is the authority on which repositories are reachable. Signing in
with Google says who you are; it grants nothing here, and a repository the
installation does not list cannot be imported however it is named.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import current_user, get_db_session, get_settings_dep
from backend.github.client import build_github_client
from backend.github.repository_source import GitHubRepositorySource
from backend.models import GitHubInstallation, Project, Repository, RunState, User
from backend.observability.logging import get_logger
from backend.orchestration.onboarding import OnboardingResult, onboard_project
from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.shared.config import GeminiConfig, Settings
from backend.shared.errors import NotFound, ValidationFailed
from backend.shared.model_provider import build_model_provider

logger = get_logger(__name__)

router = APIRouter(tags=["onboarding"])


class AuthorizedRepository(BaseModel):
    full_name: str
    default_branch: str
    installation_id: int


class ImportRequest(BaseModel):
    """What to import.

    `local_path` is optional and consequential: Continuity reads a repository
    through the GitHub App, which gives it contents but not a working tree. A
    migration needs somewhere to run tests, so a project without a checkout is
    monitored and assessed but never migrated — and the API says so rather than
    accepting the import and failing silently later.
    """

    full_name: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    local_path: str | None = Field(default=None, max_length=4096)


class ProjectCreated(BaseModel):
    project_id: uuid.UUID
    name: str
    repository: str
    state: RunState
    can_migrate: bool
    note: str


@router.get("/repositories", response_model=list[AuthorizedRepository])
async def list_authorized_repositories(
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> list[AuthorizedRepository]:
    """Repositories this user's GitHub App installation authorizes.

    Read from GitHub, never from a local list: the installation is the
    authority on what Continuity may touch, and a cached copy could outlive a
    revoked authorization.
    """
    installations = list(
        (
            await session.execute(
                select(GitHubInstallation).where(GitHubInstallation.user_id == user.id)
            )
        ).scalars()
    )

    found: list[AuthorizedRepository] = []
    for installation in installations:
        client = build_github_client(settings, installation.installation_id)
        try:
            for repository in await client.list_repositories():
                found.append(
                    AuthorizedRepository(
                        full_name=repository.full_name,
                        default_branch=repository.default_branch,
                        installation_id=installation.installation_id,
                    )
                )
        finally:
            await client.aclose()

    return sorted(found, key=lambda r: r.full_name)


@router.post(
    "/repositories/import",
    response_model=ProjectCreated,
    status_code=status.HTTP_201_CREATED,
)
async def import_repository(
    body: ImportRequest,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ProjectCreated:
    """Create a project for an authorized repository."""
    owner, _, name = body.full_name.partition("/")

    installation = await _installation_authorizing(session, settings, user, body.full_name)

    repository = (
        await session.execute(
            select(Repository).where(Repository.owner == owner, Repository.name == name)
        )
    ).scalar_one_or_none()

    if repository is None:
        repository = Repository(owner=owner, name=name)
        session.add(repository)
        await session.flush()

    repository.installation_id = installation.id
    if body.local_path:
        # Resolved off the event loop: a path on a slow or network mount would
        # otherwise block every other request while it is stat-ed.
        checkout = await asyncio.to_thread(_validated_checkout, body.local_path)
        repository.local_path = str(checkout)

    existing = (
        await session.execute(
            select(Project).where(
                Project.user_id == user.id, Project.repository_id == repository.id
            )
        )
    ).scalar_one_or_none()

    project = existing or Project(
        user_id=user.id,
        repository_id=repository.id,
        name=name,
        state=RunState.PROJECT_CREATED,
    )
    if existing is None:
        session.add(project)
    await session.flush()

    can_migrate = bool(repository.local_path)
    logger.info(
        "continuity.repository_imported",
        extra={
            "project_id": str(project.id),
            "repository": body.full_name,
            "can_migrate": can_migrate,
        },
    )

    return ProjectCreated(
        project_id=project.id,
        name=project.name,
        repository=body.full_name,
        state=project.state,
        can_migrate=can_migrate,
        note=(
            "Scan this project to map its integrations and start monitoring."
            if can_migrate
            else (
                "Scan this project to start monitoring. Migrations need a local "
                "checkout; without one Continuity will detect and assess changes "
                "but not open pull requests."
            )
        ),
    )


class ScanResponse(BaseModel):
    project_id: uuid.UUID
    state: RunState
    monitorable: bool
    files_indexed: int
    graph_version: int | None
    confirmed_nodes: int
    inferred_workflows: int
    providers: int
    mapping_degraded: str | None


@router.post("/projects/{project_id}/scan", response_model=ScanResponse)
async def scan_project(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> ScanResponse:
    """Scan, extract, map, and baseline — the front half, as one call.

    Synchronous on purpose for now: the caller learns whether the project is
    monitorable rather than being handed a job id and left to poll. A large
    repository will make this slow, and moving it behind the job queue is a
    known follow-up rather than a hidden one.
    """
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise NotFound("That project does not exist.")

    repository = await session.get(Repository, project.repository_id)
    if repository is None:  # pragma: no cover - foreign key
        raise NotFound("That project has no repository.")

    source: Any
    if repository.local_path:
        source = LocalRepositoryAdapter(Path(repository.local_path))
    else:
        source = await _github_source(session, settings, repository)

    gemini = settings.gemini
    model_provider = (
        build_model_provider(settings) if isinstance(gemini, GeminiConfig) else None
    )

    result: OnboardingResult = await onboard_project(
        session, project, source, model_provider=model_provider
    )

    summary = result.summary()
    return ScanResponse(
        project_id=project.id,
        state=result.final_state,
        monitorable=result.monitorable,
        files_indexed=int(summary["files_indexed"] or 0),
        graph_version=result.mapping.graph_version if result.mapping else None,
        confirmed_nodes=result.mapping.confirmed_nodes if result.mapping else 0,
        inferred_workflows=result.mapping.inferred_workflows if result.mapping else 0,
        providers=len(result.baseline.entries) if result.baseline else 0,
        mapping_degraded=result.mapping.agent_skipped_reason if result.mapping else None,
    )


def _validated_checkout(raw: str) -> Path:
    """A real git checkout, or a refusal.

    Checked rather than trusted: a path that is not a working tree would fail
    much later, inside a worktree creation nobody is watching.
    """
    checkout = Path(raw)
    if not (checkout / ".git").is_dir():
        raise ValidationFailed(
            f"{raw} is not a git checkout; migrations need one to run tests in."
        )
    return checkout.resolve()


async def _installation_authorizing(
    session: AsyncSession, settings: Settings, user: User, full_name: str
) -> GitHubInstallation:
    """The installation that authorizes this repository, or a refusal.

    Checked against GitHub rather than against what the request claims. This is
    the boundary between "signed in" and "may touch this code".
    """
    installations = list(
        (
            await session.execute(
                select(GitHubInstallation).where(GitHubInstallation.user_id == user.id)
            )
        ).scalars()
    )

    for installation in installations:
        client = build_github_client(settings, installation.installation_id)
        try:
            authorized = {r.full_name for r in await client.list_repositories()}
        finally:
            await client.aclose()
        if full_name in authorized:
            return installation

    raise NotFound(
        f"{full_name} is not authorized for any of your GitHub App installations."
    )


async def _github_source(
    session: AsyncSession, settings: Settings, repository: Repository
) -> GitHubRepositorySource:
    installation = (
        await session.get(GitHubInstallation, repository.installation_id)
        if repository.installation_id
        else None
    )
    if installation is None:
        raise ValidationFailed(
            "This repository has no GitHub App installation and no local "
            "checkout, so there is nothing to scan."
        )

    client = build_github_client(settings, installation.installation_id)
    source = GitHubRepositorySource(
        client, repository.owner, repository.name, repository.default_branch
    )
    return await source.load()


# ---------------------------------------------------------------------------
# Running a pass by hand
# ---------------------------------------------------------------------------


class RunStage(BaseModel):
    """One stage of a pass, and whether it did anything."""

    name: str
    detail: str


class RunNowResponse(BaseModel):
    """What one pass of the whole product did, in the order it did it."""

    project_id: uuid.UUID
    #: Providers this project depends on. Not the same as the next field: a
    #: provider with no adapter is looked at and not monitored, and reporting
    #: only this one would say "1 provider checked" when nothing was checked.
    providers_seen: int
    providers_monitored: int
    #: Why each unmonitored provider was skipped, in the monitor's own words.
    unmonitored: list[str]
    changes_recorded: int
    relevant: int
    runs: list[dict[str, Any]]
    pull_requests: list[int]
    stopped_at: str
    stages: list[RunStage]
    #: What this deployment could not do, and why. Empty is the interesting case.
    limitations: list[str]


@router.post("/projects/{project_id}/run", response_model=RunNowResponse)
async def run_now(
    project_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> RunNowResponse:
    """Run one pipeline pass now, instead of waiting for the scheduler.

    The scheduler sweeps on an interval — an hour by default — which is right
    for running unattended and useless for watching the product work. This is
    the same `run_pipeline` call the scheduler makes, with the same
    collaborators, triggered by a person.

    It reports what it *could not* do as plainly as what it did. A pass that
    checked no providers because none are configured looks identical to a pass
    that found nothing, and those are very different answers.
    """
    from backend.orchestration.in_flight import MONITORABLE, exclusive_pass
    from backend.orchestration.pipeline import run_pipeline
    from backend.orchestration.runtime import pipeline_collaborators
    from backend.providers.registry import registry
    from backend.workers.runner import project_workspace_manager

    project = await session.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise NotFound("That project does not exist.")

    repository = await session.get(Repository, project.repository_id)
    if repository is None:  # pragma: no cover - foreign key
        raise NotFound("That project has no repository.")

    limitations: list[str] = []
    if not registry.ids():
        limitations.append(
            "No provider adapter is registered, so no provider is being "
            "monitored. Set PROVIDER_SPECS."
        )

    # A refusal, not a failure, and it keeps its own 503: a pass without a model
    # cannot judge whether a change is relevant, and one that ran anyway would
    # either guess or migrate for every release. The operator needs to add
    # configuration, which is exactly what this status means.
    collaborators = await pipeline_collaborators(settings)

    workspaces = await project_workspace_manager(settings, project_id)
    if workspaces is None:
        limitations.append(
            "This project has no local checkout, so a pass stops before "
            "migrating. Re-import it with a local_path."
        )

    client = await _client_for(session, settings, repository)
    if client is None:
        limitations.append(
            "No GitHub App client is available, so a pass stops before opening "
            "a pull request."
        )

    if project.state not in MONITORABLE:
        # The same gate the scheduler applies. A project mid-migration, waiting
        # on an approval, or never scanned is not one a fresh pass should be
        # started on — the run in flight owns the workspace and the state.
        raise ValidationFailed(
            f"This project is {project.state.value}, so a new pass cannot start. "
            "A pass runs from monitoring; finish or resolve the run in flight "
            "first."
        )

    try:
        # Refused rather than queued if one is already running — by hand or by
        # the scheduler. Two pipelines on one project fight over the same
        # workspace and open competing migration runs.
        async with exclusive_pass(project_id):
            result = await run_pipeline(
                session,
                project,
                workspaces=workspaces,
                adapter_registry=registry,
                github_client=client,
                default_branch=repository.default_branch,
                **collaborators,
            )
    finally:
        if client is not None:
            await client.aclose()

    monitored = [item for item in result.monitored if item.skipped_reason is None]
    return RunNowResponse(
        project_id=project.id,
        providers_seen=len(result.monitored),
        providers_monitored=len(monitored),
        unmonitored=[
            f"{item.provider_id}: {item.skipped_reason}"
            for item in result.monitored
            if item.skipped_reason is not None
        ],
        changes_recorded=result.changes_recorded,
        relevant=result.relevant,
        runs=[run.summary() for run in result.runs],
        pull_requests=result.pull_requests,
        stopped_at=result.stopped_at,
        stages=_stages(result),
        limitations=limitations,
    )


def _stages(result: Any) -> list[RunStage]:
    """The pass as a person would narrate it.

    Derived from the result rather than emitted during the run, so this cannot
    claim a stage happened that did not.
    """
    monitored = [item for item in result.monitored if item.skipped_reason is None]
    stages = [
        RunStage(
            name="monitor",
            detail=(
                f"{len(monitored)} of {len(result.monitored)} provider(s) "
                f"monitored, {result.changes_recorded} change(s) recorded"
            ),
        ),
        RunStage(
            name="assess",
            detail=f"{result.relevant} change(s) affect this project",
        ),
    ]
    for run in result.runs:
        summary = run.summary()
        stages.append(
            RunStage(
                name=f"run {summary['provider_id']}",
                detail=f"{summary['final_state']} — {summary['reason']}",
            )
        )
    if result.stopped_at:
        stages.append(RunStage(name="stopped", detail=result.stopped_at))
    return stages


async def _client_for(
    session: AsyncSession, settings: Settings, repository: Repository
) -> Any | None:
    """A GitHub client for this repository's installation, if there is one."""
    from backend.models import GitHubInstallation
    from backend.orchestration.runtime import github_client_for

    if repository.installation_id is None:
        return None
    installation = await session.get(GitHubInstallation, repository.installation_id)
    if installation is None:
        return None
    return github_client_for(settings, installation.installation_id)
