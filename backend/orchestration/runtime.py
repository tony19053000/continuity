"""Assembling the collaborators a pipeline pass needs, at the moment it runs.

The pipeline takes its dependencies as arguments — a model provider, a workspace
manager, a GitHub client — so that a scheduler, a test, or a future AgentCore
runtime can each supply their own. This is the assembly the *application* uses.

Built fresh per pass rather than held on the scheduler, for one specific reason:
a GitHub installation token expires in an hour, and a long-lived scheduler
holding one would start failing silently overnight.

A deployment missing a piece degrades rather than fails. With no GitHub App the
pipeline does all the work and stops before delivery; with no Gemini key it does
not start at all, because judging relevance without a model would mean either
guessing or treating every change as relevant.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import gettempdir
from typing import Any

from backend.github.client import GitHubAppClient, build_github_client
from backend.migrations.workspace import WorkspaceManager
from backend.observability.execution_audit import NullExecutionAudit
from backend.observability.logging import get_logger
from backend.orchestration.coordinator import RunCoordinator
from backend.shared.config import GeminiConfig, Settings
from backend.shared.errors import IntegrationNotConfigured
from backend.shared.model_provider import build_model_provider

logger = get_logger(__name__)


def workspace_root(settings: Settings) -> Path:
    """Where migration worktrees live.

    A path, not a credential, so a default is a documented convenience. It is
    created on demand and swept at startup.
    """
    configured = settings.WORKSPACE_ROOT.strip()
    root = Path(configured) if configured else Path(gettempdir()) / "continuity-workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def pipeline_collaborators(settings: Settings) -> dict[str, Any]:
    """Everything `run_pipeline` needs, assembled for one pass.

    Raises `IntegrationNotConfigured` when the model is absent, because a pass
    without one cannot judge relevance — and a pipeline that ran anyway would
    either guess or migrate for every provider release.
    """
    gemini = settings.gemini
    if not isinstance(gemini, GeminiConfig):
        raise IntegrationNotConfigured("Gemini", gemini.reason)

    return {
        "model_provider": build_model_provider(settings),
        "coordinator": RunCoordinator(max_repair_attempts=settings.MAX_REPAIR_ATTEMPTS),
        "max_attempts": settings.MAX_REPAIR_ATTEMPTS,
    }


def github_client_for(settings: Settings, installation_id: int) -> GitHubAppClient | None:
    """A client for one installation, or None when the App is unconfigured.

    None rather than raising: a deployment without the GitHub App is a valid
    one. It does everything up to delivery and stops there, which the run
    records in words.
    """
    try:
        return build_github_client(settings, installation_id)
    except IntegrationNotConfigured as exc:
        logger.info(
            "continuity.github_unavailable", extra={"reason": exc.detail.get("reason")}
        )
        return None


def workspace_manager(
    settings: Settings, source_repo: Path, *, audit: Any | None = None
) -> WorkspaceManager:
    return WorkspaceManager(
        source_repo,
        workspace_root=workspace_root(settings),
        audit=audit or NullExecutionAudit(),
    )


__all__ = [
    "github_client_for",
    "pipeline_collaborators",
    "workspace_manager",
    "workspace_root",
]
