"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from backend.api.auth import router as auth_router
from backend.api.auth.session import OAUTH_STATE_MAX_AGE
from backend.api.routers import health, onboarding, projects
from backend.approvals import api as approvals_api
from backend.github import webhooks as github_webhooks
from backend.models.session import dispose_engine, init_engine
from backend.observability.logging import configure_logging
from backend.providers.registry import register_configured_providers
from backend.shared.config import (
    Environment,
    GeminiConfig,
    GoogleOAuthConfig,
    Settings,
    get_settings,
)
from backend.shared.errors import ContinuityError
from backend.workers.runner import build_scheduler, sweep_orphan_workspaces

logger = logging.getLogger(__name__)


def _resolve_version() -> str:
    try:
        return package_version("continuity")
    except PackageNotFoundError:  # pragma: no cover - only when not installed
        return "0.0.0+unknown"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Accepts an explicit `Settings` so tests can construct an app against a
    temporary database without mutating process-wide state.
    """
    resolved = settings or get_settings()
    configure_logging(resolved.LOG_LEVEL)
    app_version = _resolve_version()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(resolved)
        logger.info("continuity.startup", extra={"settings": repr(resolved)})

        # Monitoring is the product's autonomous half, and until this existed
        # nothing ran it: `ProviderScheduler` was built, tested, and never
        # started. It runs in-process here; production may replace it with SQS,
        # Step Functions, or AgentCore Runtime without changing `run_pipeline`.
        #
        # Disabled when the model is unconfigured — a pass cannot judge
        # relevance without one — and when explicitly switched off, which is
        # how a test or a one-off API process opts out.
        # Adapters first: a scheduler with an empty registry sweeps every
        # project and finds every provider unmonitored.
        register_configured_providers(resolved)

        scheduler = None
        if resolved.SCHEDULER_ENABLED and isinstance(resolved.gemini, GeminiConfig):
            await sweep_orphan_workspaces(resolved)
            scheduler = build_scheduler(resolved)
            await scheduler.start()
            app.state.scheduler = scheduler
        else:
            logger.info(
                "continuity.scheduler_not_started",
                extra={
                    "enabled": resolved.SCHEDULER_ENABLED,
                    "reason": (
                        "disabled by configuration"
                        if not resolved.SCHEDULER_ENABLED
                        else "no model provider is configured"
                    ),
                },
            )

        try:
            yield
        finally:
            if scheduler is not None:
                # Awaited, not just cancelled: a pass in flight holds a git
                # worktree and may have child processes.
                await scheduler.stop()
            await dispose_engine()
            logger.info("continuity.shutdown")

    app = FastAPI(
        title="Continuity",
        description="Autonomous integration reliability platform",
        version=app_version,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.version = app_version

    # The frontend runs on its own origin, so browser requests to the API are
    # cross-origin. `allow_credentials` is required because the session is an
    # HttpOnly cookie — and it is why `allow_origins` names one exact origin
    # rather than a wildcard: the two are mutually exclusive in the CORS spec,
    # and a wildcard here would let any site read authenticated responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )

    # Authlib's Starlette integration stores the OAuth nonce and code verifier
    # in `request.session`, which does not exist without this middleware — the
    # authorization flow raises immediately otherwise. It is installed only when
    # sign-in is configured, so an unconfigured deployment gets a clean 503 from
    # the route rather than a middleware that cannot be keyed.
    #
    # This cookie is separate from Continuity's own session cookie: it is
    # short-lived transport for the OAuth handshake, and carries no identity.
    google = resolved.google_oauth
    if isinstance(google, GoogleOAuthConfig):
        app.add_middleware(
            SessionMiddleware,
            secret_key=google.session_secret.get_secret_value(),
            session_cookie="continuity_oauth_transient",
            max_age=int(OAUTH_STATE_MAX_AGE.total_seconds()),
            same_site="lax",
            https_only=resolved.CONTINUITY_ENV is Environment.PRODUCTION,
        )

    @app.exception_handler(ContinuityError)
    async def handle_continuity_error(request: Request, exc: ContinuityError) -> JSONResponse:
        """Render a typed error.

        The response carries a stable code and a safe message. The traceback is
        logged, never returned — `03_SECURITY_ACCESS.md` §2.
        """
        logger.warning(
            "continuity.error",
            extra={"code": exc.code, "path": request.url.path},
            exc_info=exc,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_response())

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Last resort: log fully, reveal nothing."""
        logger.exception("continuity.unhandled", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "An internal error occurred."},
        )

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(onboarding.router)
    app.include_router(auth_router.router)
    app.include_router(approvals_api.router)
    app.include_router(github_webhooks.router)
    return app
