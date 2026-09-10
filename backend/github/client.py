"""GitHub App client — read paths only, for Phase 3.

The most important property of this class is what it does not have. There is no
method to force push, rewrite history, write to a default branch, or merge a
pull request. Those operations are not guarded by a flag that could be flipped;
they are absent, and `tests/security/test_github_surface.py` enumerates the
public surface to keep them absent.

Branch creation and pull request delivery arrive in C8-03, in a separate
`delivery` module that will carry its own refusals. Keeping reads and writes in
different classes means an analysis code path physically cannot reach a write.

Tokens: installation tokens are minted per operation, held in memory, and never
persisted or logged (`03_SECURITY_ACCESS.md` §3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from backend.observability.logging import get_logger
from backend.shared.config import GitHubAppConfig, Settings
from backend.shared.errors import ContinuityError, IntegrationNotConfigured

logger = get_logger(__name__)

GITHUB_API: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"

#: Installation tokens last an hour; refresh early so a long scan cannot expire
#: mid-run.
TOKEN_REFRESH_MARGIN_SECONDS: Final = 300


class GitHubError(ContinuityError):
    code = "github_error"
    status_code = 502
    message = "The GitHub API request failed."


class RepositoryNotAuthorized(ContinuityError):
    """A repository outside this installation's grant was requested.

    Checked on every call rather than once at import, so revoking access takes
    effect immediately (`03_SECURITY_ACCESS.md` §3).
    """

    code = "repository_not_authorized"
    status_code = 403
    message = "Continuity is not authorized for that repository."

    def __init__(self, full_name: str) -> None:
        super().__init__(
            f"Repository {full_name!r} is not in this installation's authorized set.",
            repository=full_name,
        )


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    id: int
    owner: str
    name: str
    default_branch: str
    private: bool

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(slots=True)
class _CachedToken:
    value: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return time.time() < self.expires_at - TOKEN_REFRESH_MARGIN_SECONDS


class GitHubAppClient:
    """Read-only access to the repositories one installation authorized."""

    def __init__(
        self,
        config: GitHubAppConfig,
        installation_id: int,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._installation_id = installation_id
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._token: _CachedToken | None = None
        self._authorized: frozenset[str] | None = None

    # --- authentication ---

    def _app_jwt(self) -> str:
        """Sign a short-lived app JWT with the App private key.

        Imported lazily so the module loads without PyJWT present; the GitHub
        App is blocker B-02 and the rest of the system must not depend on it.
        """
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - optional until B-02 clears
            raise GitHubError(
                "PyJWT is required for GitHub App authentication."
            ) from exc

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._config.app_id}
        key = self._config.private_key_path.read_text()
        return str(jwt.encode(payload, key, algorithm="RS256"))

    async def _installation_token(self) -> str:
        """Mint or reuse an installation token. Never persisted, never logged."""
        if self._token is not None and self._token.usable:
            return self._token.value

        response = await self._http.post(
            f"{GITHUB_API}/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if response.status_code >= 400:
            raise GitHubError(f"Could not mint an installation token ({response.status_code}).")

        body = response.json()
        # Tokens expire in an hour; treat a missing expiry conservatively.
        self._token = _CachedToken(value=body["token"], expires_at=time.time() + 3600)
        return self._token.value

    async def _get(self, path: str, **params: Any) -> Any:
        token = await self._installation_token()
        response = await self._http.get(
            f"{GITHUB_API}{path}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            params=params or None,
        )
        if response.status_code >= 400:
            # The message carries the path and status, never the token.
            raise GitHubError(f"GitHub returned {response.status_code} for {path}.")
        return response.json()

    # --- authorized repositories ---

    async def list_repositories(self) -> list[GitHubRepository]:
        """Every repository this installation may access."""
        body = await self._get("/installation/repositories", per_page=100)
        repositories = [
            GitHubRepository(
                id=item["id"],
                owner=item["owner"]["login"],
                name=item["name"],
                default_branch=item.get("default_branch", "main"),
                private=item.get("private", True),
            )
            for item in body.get("repositories", [])
        ]
        self._authorized = frozenset(repo.full_name for repo in repositories)
        return repositories

    async def _require_authorized(self, full_name: str) -> None:
        """Re-validate the boundary on every call.

        Not cached-and-trusted: an installation can be revoked between the
        listing and the read, and failing closed is the only safe default.
        """
        if self._authorized is None:
            await self.list_repositories()
        if full_name not in (self._authorized or frozenset()):
            raise RepositoryNotAuthorized(full_name)

    # --- reads ---

    async def get_repository(self, owner: str, name: str) -> GitHubRepository:
        await self._require_authorized(f"{owner}/{name}")
        body = await self._get(f"/repos/{owner}/{name}")
        return GitHubRepository(
            id=body["id"],
            owner=body["owner"]["login"],
            name=body["name"],
            default_branch=body.get("default_branch", "main"),
            private=body.get("private", True),
        )

    async def list_branches(self, owner: str, name: str) -> list[str]:
        await self._require_authorized(f"{owner}/{name}")
        body = await self._get(f"/repos/{owner}/{name}/branches", per_page=100)
        return [item["name"] for item in body]

    async def get_tree(self, owner: str, name: str, ref: str) -> list[dict[str, Any]]:
        """Flat file listing at `ref`. Blobs only."""
        await self._require_authorized(f"{owner}/{name}")
        body = await self._get(f"/repos/{owner}/{name}/git/trees/{ref}", recursive="1")
        return [item for item in body.get("tree", []) if item.get("type") == "blob"]

    async def read_file(self, owner: str, name: str, path: str, ref: str) -> str:
        """File contents at `ref`, boundary- and exclusion-checked.

        `guard_readable` runs before the request, so an excluded path costs no
        network call and, more importantly, cannot be fetched at all.
        """
        from backend.repository.source import guard_readable

        await self._require_authorized(f"{owner}/{name}")
        normalized = guard_readable(path)

        body = await self._get(f"/repos/{owner}/{name}/contents/{normalized}", ref=ref)
        if body.get("encoding") != "base64":
            raise GitHubError(f"Unexpected encoding for {normalized!r}.")

        import base64

        return base64.b64decode(body["content"]).decode("utf-8", errors="replace")

    async def aclose(self) -> None:
        await self._http.aclose()


def build_github_client(settings: Settings, installation_id: int) -> GitHubAppClient:
    """Construct a client, or raise naming exactly what is missing."""
    config = settings.github_app
    if isinstance(config, GitHubAppConfig):
        return GitHubAppClient(config, installation_id)
    raise IntegrationNotConfigured("GitHub App", config.reason)
