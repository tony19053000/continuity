"""Google sign-in.

Signing into Continuity grants access to Continuity and nothing else. It does
**not** grant access to any repository — that is a separate, explicit GitHub App
installation (`03_SECURITY_ACCESS.md` §3). The `/auth/me` response says so
directly so the frontend cannot imply otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth.session import (
    OAUTH_STATE_COOKIE,
    OAUTH_STATE_MAX_AGE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    SessionError,
    cookie_kwargs,
    is_safe_redirect,
    issue_oauth_state,
    issue_session,
    read_oauth_state,
)
from backend.api.deps import (
    CurrentUserDep,
    SessionDep,
    get_db_session,
    get_settings_dep,
)
from backend.models import GitHubInstallation, User
from backend.shared.config import GoogleOAuthConfig, Settings
from backend.shared.errors import (
    AuthenticationRequired,
    IntegrationNotConfigured,
    ValidationFailed,
)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"


class CurrentUser(BaseModel):
    id: str
    email: str
    display_name: str | None
    avatar_url: str | None
    # Stated explicitly: authentication is not repository authorization.
    github_connected: bool = False


def _require_google(settings: Settings) -> GoogleOAuthConfig:
    """Google configuration, or a 503 naming what is missing.

    Narrowing with `isinstance` rather than an `assert` matters: assertions are
    stripped under `python -O`, which would turn a configuration error into an
    `AttributeError` deep inside the OAuth client.
    """
    auth = settings.google_oauth
    if isinstance(auth, GoogleOAuthConfig):
        return auth
    raise IntegrationNotConfigured("Google sign-in", auth.reason)


def _oauth_client(auth: GoogleOAuthConfig) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=auth.client_id,
        client_secret=auth.client_secret.get_secret_value(),
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


@router.get("/login")
async def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    redirect_to: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Begin the authorization-code flow.

    The signed `state` is stored in its own short-lived cookie and compared on
    callback, which is what prevents a third party from completing a sign-in the
    user never started.
    """
    auth = _require_google(settings)

    if not is_safe_redirect(redirect_to):
        raise ValidationFailed("redirect_to must be a site-relative path.")

    state = issue_oauth_state(auth, redirect_to)

    oauth = _oauth_client(auth)
    redirect_uri = str(request.url_for("auth_callback"))
    response: RedirectResponse = await oauth.google.authorize_redirect(
        request, redirect_uri, state=state
    )
    response.set_cookie(
        OAUTH_STATE_COOKIE, state, **cookie_kwargs(settings, OAUTH_STATE_MAX_AGE)
    )
    return response


@router.get("/callback", name="auth_callback")
async def callback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RedirectResponse:
    """Complete sign-in and issue a session."""
    auth = _require_google(settings)

    submitted_state = request.query_params.get("state")
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not submitted_state or submitted_state != cookie_state:
        raise AuthenticationRequired("OAuth state did not match.")

    try:
        redirect_to = read_oauth_state(auth, submitted_state)
    except SessionError as exc:
        raise AuthenticationRequired("OAuth state was not valid.") from exc

    oauth = _oauth_client(auth)
    token: dict[str, Any] = await oauth.google.authorize_access_token(request)
    claims = token.get("userinfo") or {}
    subject = claims.get("sub")
    email = claims.get("email")
    if not subject or not email:
        raise AuthenticationRequired("Google did not return an identity.")

    user = await upsert_user(
        session,
        subject=subject,
        email=email,
        display_name=claims.get("name"),
        avatar_url=claims.get("picture"),
    )

    # Re-checked after unsigning: the value was validated when the flow started,
    # and validating it again here means a single missed check cannot open a
    # redirect.
    destination = redirect_to if is_safe_redirect(redirect_to) else "/"

    response = RedirectResponse(url=destination or "/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(auth, user.id, user.session_version),
        **cookie_kwargs(settings, SESSION_MAX_AGE),
    )
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")
    return response


async def upsert_user(
    session: AsyncSession,
    *,
    subject: str,
    email: str,
    display_name: str | None,
    avatar_url: str | None,
) -> User:
    """Create or refresh the user for a Google subject.

    Keyed on the subject rather than the email, because Google's subject is
    stable across email changes — keying on email would silently create a second
    account, orphaning that user's projects and approvals.
    """
    existing = (
        await session.execute(select(User).where(User.google_subject == subject))
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if existing is not None:
        existing.email = email
        existing.display_name = display_name
        existing.avatar_url = avatar_url
        existing.last_login_at = now
        await session.flush()
        return existing

    user = User(
        google_subject=subject,
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
        last_login_at=now,
    )
    session.add(user)
    await session.flush()
    return user


@router.post("/logout")
async def logout(user: CurrentUserDep, session: SessionDep) -> JSONResponse:
    """Sign out, revoking every outstanding session for this user.

    Clearing the cookie alone would not be sign-out: the token is a bearer
    credential, so a copy captured elsewhere would stay valid for its full
    lifetime. Bumping `session_version` invalidates all of them at once.
    """
    user.session_version += 1
    await session.flush()

    response = JSONResponse({"signed_out": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=CurrentUser)
async def me(user: CurrentUserDep, session: SessionDep) -> CurrentUser:
    """The signed-in user, and whether GitHub is separately connected."""
    installations = (
        await session.execute(
            select(GitHubInstallation.id).where(
                GitHubInstallation.user_id == user.id,
                GitHubInstallation.revoked_at.is_(None),
            )
        )
    ).first()

    return CurrentUser(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        github_connected=installations is not None,
    )
