"""Session handling.

The browser receives a signed, opaque cookie carrying a user id, a session
version, and an expiry — nothing else. It is `HttpOnly` (so JavaScript cannot
read it), `SameSite=Lax` (so it is not sent on cross-site POSTs), and `Secure`
outside development.

The token is a stateless bearer credential: authenticity comes from the
signature rather than from a stored session row. Revocation therefore works
through the version field (see `issue_session`) rather than by deleting server
state.

Signing uses `itsdangerous`, which produces a tamper-evident token. A user
cannot edit the cookie to become another user: any modification invalidates the
signature and the session is rejected.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Final, NamedTuple

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.shared.config import Environment, GoogleOAuthConfig, Settings

SESSION_COOKIE: Final = "continuity_session"
OAUTH_STATE_COOKIE: Final = "continuity_oauth_state"

SESSION_MAX_AGE: Final = timedelta(days=14)
OAUTH_STATE_MAX_AGE: Final = timedelta(minutes=10)

_SESSION_SALT: Final = "continuity.session.v1"
_STATE_SALT: Final = "continuity.oauth-state.v1"


class SessionError(Exception):
    """A session cookie was absent, malformed, tampered with, or expired."""


def _serializer(auth: GoogleOAuthConfig, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(auth.session_secret.get_secret_value(), salt=salt)


class SessionPayload(NamedTuple):
    user_id: uuid.UUID
    version: int


def issue_session(auth: GoogleOAuthConfig, user_id: uuid.UUID, version: int = 0) -> str:
    """Mint a session token for `user_id` at their current session version.

    `version` is what makes sign-out real. The token is a bearer credential, so
    deleting the browser's copy cannot revoke a copy taken elsewhere; binding
    the token to a counter on the user row means bumping that counter
    invalidates every token issued before it.
    """
    return _serializer(auth, _SESSION_SALT).dumps(
        {"user_id": str(user_id), "version": version}
    )


def read_session(auth: GoogleOAuthConfig, token: str) -> SessionPayload:
    """Return the identity in `token`, or raise `SessionError`.

    Every failure mode collapses to the same exception so that callers cannot
    accidentally distinguish "expired" from "forged" and leak that difference to
    an attacker.

    Verifying `version` against the stored user is the caller's job — this
    function only proves the token is authentic and unexpired.
    """
    try:
        payload: dict[str, Any] = _serializer(auth, _SESSION_SALT).loads(
            token, max_age=int(SESSION_MAX_AGE.total_seconds())
        )
        return SessionPayload(
            user_id=uuid.UUID(payload["user_id"]),
            version=int(payload.get("version", 0)),
        )
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError) as exc:
        raise SessionError("Invalid session") from exc


def is_safe_redirect(target: str | None) -> bool:
    """Whether `target` is a same-site path we may redirect to after sign-in.

    Only site-relative paths are accepted. A signed `state` proves *we* minted
    the value, not that the destination is ours — without this check,
    `/auth/login?redirect_to=https://evil.example` would hand a freshly
    authenticated user straight to an attacker.

    Rejected: absolute URLs, protocol-relative `//host`, backslash variants that
    some browsers normalise to `//`, and any C0/C1 control character or Unicode
    line separator — the last group cannot smuggle a redirect past Starlette's
    percent-encoding, but a control character in a Location header is never
    something we meant to emit.
    """
    if target is None:
        return True
    if not target.startswith("/"):
        return False
    if target.startswith(("//", "/\\")):
        return False
    return not any(_is_control(char) for char in target)


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F or char in ("\u2028", "\u2029")


def issue_oauth_state(auth: GoogleOAuthConfig, redirect_to: str | None = None) -> str:
    """Create the CSRF state value for an authorization request.

    Signed and time-limited, so a state value cannot be minted by a third party
    or replayed later.
    """
    return _serializer(auth, _STATE_SALT).dumps({"redirect_to": redirect_to})


def read_oauth_state(auth: GoogleOAuthConfig, token: str) -> str | None:
    try:
        payload: dict[str, Any] = _serializer(auth, _STATE_SALT).loads(
            token, max_age=int(OAUTH_STATE_MAX_AGE.total_seconds())
        )
        return payload.get("redirect_to")
    except (BadSignature, SignatureExpired, ValueError, TypeError) as exc:
        raise SessionError("Invalid OAuth state") from exc


def cookie_kwargs(settings: Settings, max_age: timedelta) -> dict[str, Any]:
    """Cookie attributes.

    `secure` is relaxed only in development and test, where there is no TLS
    terminator in front of the app. It is never relaxed in production.
    """
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.CONTINUITY_ENV is Environment.PRODUCTION,
        "max_age": int(max_age.total_seconds()),
        "path": "/",
    }
