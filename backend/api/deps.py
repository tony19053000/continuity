"""FastAPI dependencies.

`current_user` is the single place a request is turned into an identity. Every
protected route depends on it.

It is also the intended source of the approver identity: when the approval
service arrives (C8-02), it will accept only a `User` produced here, which is
how `03_SECURITY_ACCESS.md` §4's "only an authenticated human can approve"
becomes true in code rather than only in prose. That service does not exist
yet — today this module's job is simply to make an authenticated `User` the
only thing a protected route can obtain.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.auth.session import SESSION_COOKIE, SessionError, read_session
from backend.models import User
from backend.models.session import get_session_factory
from backend.shared.config import GoogleOAuthConfig, Settings
from backend.shared.errors import AuthenticationRequired


def get_settings_dep(request: Request) -> Settings:
    """Settings for this application instance.

    Read from app state rather than the module-level cache so a test can build
    an app with its own settings without touching global state.
    """
    settings: Settings = request.app.state.settings
    return settings


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """A transactional session, committed when the request succeeds."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> User:
    """The signed-in user, or 401.

    A session naming a user that no longer exists is rejected rather than
    tolerated, so deleting a user immediately revokes their access.
    """
    auth = settings.google_oauth
    if not isinstance(auth, GoogleOAuthConfig):
        raise AuthenticationRequired("Sign-in is not configured.")

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise AuthenticationRequired()

    try:
        payload = read_session(auth, token)
    except SessionError as exc:
        raise AuthenticationRequired() from exc

    user = await session.get(User, payload.user_id)
    if user is None:
        raise AuthenticationRequired()

    # A token minted before the user's last sign-out is refused, so signing out
    # revokes every copy of the cookie rather than only the browser's.
    if payload.version != user.session_version:
        raise AuthenticationRequired()
    return user


CurrentUserDep = Annotated[User, Depends(current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
