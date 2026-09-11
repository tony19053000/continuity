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
