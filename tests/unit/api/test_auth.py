"""C1-05 acceptance: authentication, sessions, and the boundary it does NOT cross.

The most important assertions here are negative ones. A session must be
unforgeable, unreadable by JavaScript, and useless once its user is gone —
because everything the approval system promises rests on the identity these
tests protect.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from httpx import ASGITransport, AsyncClient
from itsdangerous import URLSafeTimedSerializer

from backend.api.app import create_app
from backend.api.auth.router import upsert_user
from backend.api.auth.session import (
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    SessionError,
    is_safe_redirect,
    issue_oauth_state,
    issue_session,
    read_oauth_state,
    read_session,
)
from backend.models import User
from backend.models.session import create_all, session_scope
from backend.shared.config import Environment, GoogleOAuthConfig, Settings

SESSION_SECRET = "test-session-secret-not-a-real-one"


@pytest.fixture
def auth_settings(tmp_path: Path) -> Settings:
    """Settings with Google sign-in configured, using throwaway test values."""
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}",
        GOOGLE_OAUTH_CLIENT_ID="test-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="test-client-secret",
        SESSION_SECRET=SESSION_SECRET,
        _env_file=None,
    )


@pytest.fixture
def google(auth_settings: Settings) -> GoogleOAuthConfig:
    config = auth_settings.google_oauth
    assert isinstance(config, GoogleOAuthConfig)
    return config


@pytest.fixture
def auth_app(auth_settings: Settings) -> FastAPI:
    return create_app(auth_settings)


@pytest_asyncio.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with auth_app.router.lifespan_context(auth_app):
        await create_all()
        transport = ASGITransport(app=auth_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


# --- Session tokens ------------------------------------------------------


def test_session_round_trips(google: GoogleOAuthConfig) -> None:
    user_id = uuid.uuid4()

    payload = read_session(google, issue_session(google, user_id, version=3))

    assert payload.user_id == user_id
    assert payload.version == 3


def test_a_tampered_session_is_rejected(google: GoogleOAuthConfig) -> None:
    """Editing the cookie to become another user must fail."""
    token = issue_session(google, uuid.uuid4())
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")

    with pytest.raises(SessionError):
        read_session(google, tampered)


def test_a_session_signed_with_another_secret_is_rejected(
    google: GoogleOAuthConfig,
) -> None:
    forged = URLSafeTimedSerializer("a-different-secret", salt="continuity.session.v1").dumps(
        {"user_id": str(uuid.uuid4())}
    )

    with pytest.raises(SessionError):
        read_session(google, forged)


def test_an_expired_session_is_rejected(
    google: GoogleOAuthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.api.auth.session as session_module

    monkeypatch.setattr(session_module, "SESSION_MAX_AGE", timedelta(seconds=-1))

    with pytest.raises(SessionError):
        read_session(google, issue_session(google, uuid.uuid4()))


def test_garbage_is_rejected(google: GoogleOAuthConfig) -> None:
    with pytest.raises(SessionError):
        read_session(google, "not-a-token")


def test_oauth_state_round_trips_and_rejects_forgery(google: GoogleOAuthConfig) -> None:
    assert read_oauth_state(google, issue_oauth_state(google, "/projects")) == "/projects"

    with pytest.raises(SessionError):
        read_oauth_state(google, "forged-state")


# --- Protected routes ----------------------------------------------------


async def test_me_requires_authentication(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


async def test_me_rejects_an_invalid_session_cookie(auth_client: AsyncClient) -> None:
    auth_client.cookies.set(SESSION_COOKIE, "forged")

    assert (await auth_client.get("/auth/me")).status_code == 401


async def test_me_returns_the_signed_in_user(
    auth_client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    async with session_scope() as session:
        user = await upsert_user(
            session,
            subject="google-subject-1",
            email="dev@example.com",
            display_name="Dev",
            avatar_url=None,
        )
        user_id = user.id

    auth_client.cookies.set(SESSION_COOKIE, issue_session(google, user_id))
    response = await auth_client.get("/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "dev@example.com"


async def test_signing_in_does_not_grant_repository_access(
    auth_client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """Authentication and repository authorization are separate concepts.

    A newly signed-in user must report `github_connected: false`, so the UI
    cannot imply Continuity can already read their code.
    """
    async with session_scope() as session:
        user = await upsert_user(
            session,
            subject="google-subject-2",
            email="new@example.com",
            display_name=None,
            avatar_url=None,
        )
        user_id = user.id

    auth_client.cookies.set(SESSION_COOKIE, issue_session(google, user_id))
    body = (await auth_client.get("/auth/me")).json()

    assert body["github_connected"] is False


async def test_a_session_for_a_deleted_user_is_rejected(
    auth_client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """Deleting a user must revoke access immediately."""
    async with session_scope() as session:
        user = await upsert_user(
            session, subject="gone", email="gone@example.com", display_name=None, avatar_url=None
        )
        user_id = user.id

    async with session_scope() as session:
        await session.delete(await session.get(User, user_id))

    auth_client.cookies.set(SESSION_COOKIE, issue_session(google, user_id))

    assert (await auth_client.get("/auth/me")).status_code == 401


# --- Cookie hardening ----------------------------------------------------


async def test_logout_requires_authentication(auth_client: AsyncClient) -> None:
    """Logout mutates user state, so it cannot be called anonymously."""
    assert (await auth_client.post("/auth/logout")).status_code == 401


async def test_logout_clears_the_session_cookie(
    auth_client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    async with session_scope() as session:
        user = await upsert_user(
            session, subject="lo", email="lo@example.com", display_name=None, avatar_url=None
        )
        auth_client.cookies.set(SESSION_COOKIE, issue_session(google, user.id, user.session_version))

    response = await auth_client.post("/auth/logout")

    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE in set_cookie
    assert "Max-Age=0" in set_cookie or 'expires=Thu, 01 Jan 1970' in set_cookie.lower()


def test_session_cookie_is_httponly_and_samesite() -> None:
    from backend.api.auth.session import SESSION_MAX_AGE, cookie_kwargs

    kwargs = cookie_kwargs(
        Settings(CONTINUITY_ENV=Environment.DEVELOPMENT, _env_file=None), SESSION_MAX_AGE
    )

    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"


def test_session_cookie_is_secure_in_production() -> None:
    from backend.api.auth.session import SESSION_MAX_AGE, cookie_kwargs

    production = cookie_kwargs(
        Settings(CONTINUITY_ENV=Environment.PRODUCTION, _env_file=None), SESSION_MAX_AGE
    )
    development = cookie_kwargs(
        Settings(CONTINUITY_ENV=Environment.DEVELOPMENT, _env_file=None), SESSION_MAX_AGE
    )

    assert production["secure"] is True
    # Relaxed only where there is no TLS terminator in front of the app.
    assert development["secure"] is False


# --- Unconfigured integration -------------------------------------------


async def test_login_reports_not_configured_without_google_credentials(
    client: AsyncClient,
) -> None:
    """Absent configuration produces a clear 503, not a crash or a fake redirect."""
    response = await client.get("/auth/login")

    assert response.status_code == 503
    assert response.json()["code"] == "integration_not_configured"


async def test_callback_rejects_a_mismatched_state(auth_client: AsyncClient) -> None:
    """The CSRF guard: a callback the user never initiated must fail."""
    response = await auth_client.get("/auth/callback?state=attacker-supplied&code=abc")

    assert response.status_code == 401


# --- User upsert ---------------------------------------------------------


async def test_upsert_creates_then_updates_keyed_on_subject(database: None) -> None:
    """Google's subject is stable across email changes; the email is not.

    Keying on email would silently create a second account and orphan the
    original user's projects and approvals.
    """
    async with session_scope() as session:
        first = await upsert_user(
            session,
            subject="stable-subject",
            email="old@example.com",
            display_name="Old",
            avatar_url=None,
        )
        first_id = first.id

    async with session_scope() as session:
        second = await upsert_user(
            session,
            subject="stable-subject",
            email="new@example.com",
            display_name="New",
            avatar_url="https://example.test/a.png",
        )

        assert second.id == first_id
        assert second.email == "new@example.com"
        assert second.last_login_at is not None


# --- Session revocation --------------------------------------------------


async def test_signing_out_revokes_a_captured_cookie(
    auth_client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """Sign-out must invalidate the token, not merely clear the browser's copy.

    A session cookie is a bearer credential. If logout only deleted the local
    cookie, a copy captured anywhere else would stay valid for the full 14-day
    lifetime — which is not sign-out in any meaningful sense.
    """
    async with session_scope() as session:
        user = await upsert_user(
            session,
            subject="revoke-me",
            email="revoke@example.com",
            display_name=None,
            avatar_url=None,
        )
        token = issue_session(google, user.id, user.session_version)

    auth_client.cookies.set(SESSION_COOKIE, token)
    assert (await auth_client.get("/auth/me")).status_code == 200

    await auth_client.post("/auth/logout")

    # An attacker replaying the captured token must now be refused.
    auth_client.cookies.set(SESSION_COOKIE, token)
    assert (await auth_client.get("/auth/me")).status_code == 401


async def test_a_token_from_an_older_session_version_is_rejected(
    auth_client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """A stale version must be refused at the route, not merely readable."""
    async with session_scope() as session:
        user = await upsert_user(
            session, subject="stale", email="stale@x.test", display_name=None, avatar_url=None
        )
        user.session_version = 3
        await session.flush()
        user_id = user.id

    auth_client.cookies.set(SESSION_COOKIE, issue_session(google, user_id, version=2))
    assert (await auth_client.get("/auth/me")).status_code == 401

    auth_client.cookies.set(SESSION_COOKIE, issue_session(google, user_id, version=3))
    assert (await auth_client.get("/auth/me")).status_code == 200


async def test_a_token_claiming_a_future_session_version_is_rejected(
    auth_client: AsyncClient, google: GoogleOAuthConfig
) -> None:
    """Only an exact match is accepted, so a guessed version cannot help."""
    async with session_scope() as session:
        user = await upsert_user(
            session, subject="future", email="future@x.test", display_name=None, avatar_url=None
        )
        user_id = user.id

    auth_client.cookies.set(SESSION_COOKIE, issue_session(google, user_id, version=99))

    assert (await auth_client.get("/auth/me")).status_code == 401


# --- Open redirect -------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example",
        "//evil.example",
        "/\\evil.example",
        "http://evil.example/path",
        "javascript:alert(1)",
        "/path\r\nSet-Cookie: x=1",
        "/\x0b/evil",
        "/\x0c/evil",
        "/\x7f/evil",
        "/\x85/evil",
        "/\u2028/evil",
        "/\u2029/evil",
        "///evil.example",
        "/\\/evil.example",
    ],
)
def test_off_site_redirect_targets_are_rejected(hostile: str) -> None:
    """A signed state proves we minted the value, not that the target is ours."""
    assert is_safe_redirect(hostile) is False


@pytest.mark.parametrize("safe", [None, "/", "/projects", "/projects/1?tab=changes"])
def test_site_relative_redirect_targets_are_allowed(safe: str | None) -> None:
    assert is_safe_redirect(safe) is True


async def test_login_rejects_an_off_site_redirect(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/auth/login?redirect_to=https://evil.example")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


# --- The real sign-in flow -----------------------------------------------


class _StubGoogle:
    """Stands in for Authlib's registered Google client.

    Only the two methods the router calls are implemented. This exercises the
    router, the middleware, the state cookie, and session issuance — everything
    except Google itself.
    """

    def __init__(self, claims: dict[str, str]) -> None:
        self.claims = claims

    async def authorize_redirect(
        self, request: object, redirect_uri: str, **kwargs: object
    ) -> RedirectResponse:
        return RedirectResponse(url="https://accounts.google.com/o/oauth2/v2/auth", status_code=302)

    async def authorize_access_token(self, request: object) -> dict[str, object]:
        return {"userinfo": self.claims}


class _StubOAuth:
    def __init__(self, claims: dict[str, str]) -> None:
        self.google = _StubGoogle(claims)


async def test_login_redirects_to_google_when_configured(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: this raised `SessionMiddleware must be installed`.

    Only the *unconfigured* path was covered before, so the entire configured
    OAuth flow was broken and no test noticed.
    """
    import backend.api.auth.router as router_module

    monkeypatch.setattr(
        router_module, "_oauth_client", lambda auth: _StubOAuth({"sub": "s", "email": "e@x.test"})
    )

    response = await auth_client.get("/auth/login", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert "accounts.google.com" in response.headers["location"]
    # The CSRF state cookie must have been set for the callback to compare.
    assert OAUTH_STATE_COOKIE in response.headers.get("set-cookie", "")


async def test_full_sign_in_flow_issues_a_working_session(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login → callback → an authenticated request, through the product's own flow."""
    import backend.api.auth.router as router_module

    monkeypatch.setattr(
        router_module,
        "_oauth_client",
        lambda auth: _StubOAuth(
            {"sub": "google-sub-flow", "email": "flow@example.com", "name": "Flow"}
        ),
    )

    login = await auth_client.get("/auth/login?redirect_to=/projects", follow_redirects=False)
    state = auth_client.cookies.get(OAUTH_STATE_COOKIE)
    assert state, "login must set the state cookie"
    assert login.status_code in (302, 307)

    callback = await auth_client.get(
        f"/auth/callback?state={state}&code=stub-code", follow_redirects=False
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/projects"

    me = await auth_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "flow@example.com"
    # Signing in still grants no repository access.
    assert me.json()["github_connected"] is False


async def test_callback_ignores_an_off_site_redirect_in_a_valid_state(
    auth_client: AsyncClient, google: GoogleOAuthConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: even a correctly-signed hostile target is not followed."""
    import backend.api.auth.router as router_module

    monkeypatch.setattr(
        router_module,
        "_oauth_client",
        lambda auth: _StubOAuth({"sub": "sub-2", "email": "two@example.com"}),
    )

    # Mint a state carrying a hostile target, bypassing the /auth/login guard.
    hostile_state = issue_oauth_state(google, "https://evil.example")
    auth_client.cookies.set(OAUTH_STATE_COOKIE, hostile_state)

    response = await auth_client.get(
        f"/auth/callback?state={hostile_state}&code=x", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
