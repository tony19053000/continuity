"""The background worker the application actually starts.

`ProviderScheduler` existed and was tested and nothing started it, which meant
monitoring never happened outside a test. This is the piece that connects it to
the application lifecycle, and the place that decides what one sweep can do for
a given project.

Per project, per pass, it assembles:

* the model provider (required — without one relevance cannot be judged);
* a workspace manager, **only if the repository has a local checkout**. A
  GitHub-sourced project with none is monitored and assessed but not migrated,
  and the pipeline says so rather than opening a run it could not finish;
* a GitHub client for the installation, if the App is configured.

It also sweeps orphaned workspaces at startup and verifies merged runs after
each pass, so the loop closes without anyone asking.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.models import Project, PullRequest, Repository
from backend.models.session import session_scope
from backend.observability.execution_audit import DatabaseExecutionAudit
from backend.observability.logging import get_logger
from backend.orchestration.resume import resume_decided_runs
from backend.orchestration.runtime import (
    github_client_for,
    pipeline_collaborators,
    workspace_root,
)
from backend.shared.config import Settings
from backend.workers.post_merge import verify_merged_runs
from backend.workers.pr_status_poll import poll_open_pull_requests
from backend.workers.scheduler import ProviderScheduler

logger = get_logger(__name__)


def build_scheduler(settings: Settings) -> ProviderScheduler:
    """The scheduler the application runs."""

    async def collaborators(project_id: uuid.UUID) -> dict[str, Any]:
        # Built per pass, and per project. A GitHub installation token expires
        # in an hour, so a scheduler holding one would start failing silently
        # overnight; and the workspace depends on which repository this is.
        assembled = await pipeline_collaborators(settings)
        assembled["workspaces"] = await project_workspace_manager(settings, project_id)
        assembled["github_client"] = await _client_for_project(settings, project_id)
        return assembled

    async def follow_up() -> None:
        """After each sweep: resume approvals, detect merges, verify them.

        In that order, and all three are necessary. Webhooks are the preferred
        merge signal and are off in any deployment with no signing secret — so
        without the poll nothing ever marks a pull request merged, and
        verification would sit waiting for a state that never arrives. The loop
        would look closed and not be.
        """
        async def workspaces_for(project_id: uuid.UUID) -> Any:
            return await project_workspace_manager(settings, project_id)

        async def github_for(project_id: uuid.UUID) -> Any:
            return await _client_for_project(settings, project_id)

        async with session_scope() as session:
            await resume_decided_runs(
                session, workspaces_for=workspaces_for, github_for=github_for
            )
        await poll_for_merges(settings)
        async with session_scope() as session:
            await verify_merged_runs(session)

    return ProviderScheduler(
        interval_seconds=settings.PROVIDER_POLL_INTERVAL_SECONDS,
        pipeline_factory=collaborators,
        poll_factory=follow_up,
    )


async def poll_for_merges(settings: Settings) -> int:
    """Ask GitHub about the pull requests Continuity still believes are open.

    The fallback for deployments that cannot receive webhooks — which is every
    deployment without a signing secret, including this one. Scoped to
    repositories that actually have an open Continuity pull request, so a quiet
    project costs no API calls.
    """
    async with session_scope() as session:
        repository_ids = list(
            (
                await session.execute(
                    select(PullRequest.repository_id)
                    .where(PullRequest.state == "open")
                    .distinct()
                )
            ).scalars()
        )

    settled = 0
    for repository_id in repository_ids:
        client = await _client_for_repository(settings, repository_id)
        if client is None:
            continue
        try:
            async with session_scope() as session:
                result = await poll_open_pull_requests(
                    session, client=client, repository_id=repository_id
                )
                settled += len(result.settled)
        finally:
            await client.aclose()

    if settled:
        logger.info("continuity.merges_detected_by_poll", extra={"settled": settled})
    return settled


async def _client_for_repository(
    settings: Settings, repository_id: uuid.UUID
) -> Any | None:
    from backend.models import GitHubInstallation

    async with session_scope() as session:
        repository = await session.get(Repository, repository_id)
        if repository is None or repository.installation_id is None:
            return None
        installation = await session.get(GitHubInstallation, repository.installation_id)
        if installation is None:
            return None
        external_id = installation.installation_id

    return github_client_for(settings, external_id)


async def _client_for_project(settings: Settings, project_id: uuid.UUID) -> Any | None:
    """A GitHub client for this project's installation, if there is one.

    None is legitimate: a deployment without the App does every stage up to
    delivery and stops there, which the run records in words.
    """
    from backend.models import GitHubInstallation

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return None
        repository = await session.get(Repository, project.repository_id)
        if repository is None or repository.installation_id is None:
            return None
        installation = await session.get(GitHubInstallation, repository.installation_id)
        if installation is None:
            return None
        external_id = installation.installation_id

    return github_client_for(settings, external_id)


async def sweep_orphan_workspaces(settings: Settings) -> list[str]:
    """Reclaim worktrees a crashed process left behind.

    Run at startup, which is the one moment nothing is in progress — so the
    sweep takes no age floor and can safely remove everything it finds under
    its own prefix.
    """
    from backend.migrations.workspace import WORKSPACE_PREFIX

    root = workspace_root(settings)
    swept: list[str] = []

    for candidate in sorted(root.iterdir()):
        if candidate.is_dir() and candidate.name.startswith(WORKSPACE_PREFIX):
            import shutil

            shutil.rmtree(candidate, ignore_errors=True)
            swept.append(candidate.name)

    if swept:
        logger.warning(
            "continuity.startup_workspace_sweep",
            extra={"count": len(swept), "root": str(root)},
        )
    return swept


async def project_workspace_manager(
    settings: Settings, project_id: uuid.UUID
) -> Any | None:
    """A workspace manager for one project, or None when it has no checkout.

    None is a legitimate, common state: Continuity reads repositories through
    the GitHub App, which supplies contents but not a working tree. A migration
    needs somewhere to run tests, and there is no honest way to produce one
    without a local clone.
    """
    from backend.migrations.workspace import WorkspaceManager

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if project is None:
            return None
        repository = await session.get(Repository, project.repository_id)

    if repository is None or not repository.local_path:
        return None

    root = Path(repository.local_path)
    if not (root / ".git").exists():
        logger.warning(
            "continuity.local_checkout_missing",
            extra={"project_id": str(project_id), "path": str(root)},
        )
        return None

    async with session_scope() as session:
        return WorkspaceManager(
            root,
            workspace_root=workspace_root(settings),
            audit=DatabaseExecutionAudit(session, actor="scheduler"),
        )


async def monitorable_project_ids() -> list[uuid.UUID]:
    """Every project the scheduler would consider. Used by operations tooling."""
    async with session_scope() as session:
        return list(
            (await session.execute(select(Project.id))).scalars().all()
        )
