"""C3-02 acceptance: the GitHub client cannot perform a forbidden operation.

`03_SECURITY_ACCESS.md` §3 lists operations Continuity must never perform. The
guarantee is structural rather than conditional: there is no method to call, so
no flag, prompt, or agent can reach one.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.github.client import GitHubAppClient, RepositoryNotAuthorized
from backend.shared.config import Environment, Settings
from backend.shared.errors import IntegrationNotConfigured

FORBIDDEN_FRAGMENTS = (
    "force",
    "push",
    "merge",
    "rewrite",
    "delete",
    "reset",
    "squash",
    "rebase",
    "commit",
    "write",
    "update_ref",
    "create_branch",
)


def _public_methods(cls: type) -> list[str]:
    return sorted(
        name
        for name, _ in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    )


def test_the_public_surface_contains_no_forbidden_operation() -> None:
    methods = _public_methods(GitHubAppClient)

    offending = [
        name for name in methods if any(bad in name.lower() for bad in FORBIDDEN_FRAGMENTS)
    ]
    assert not offending, f"forbidden-shaped methods present: {offending}"


def test_the_public_surface_is_exactly_the_documented_read_set() -> None:
    """Pinned deliberately.

    A new method appearing here should require a conscious decision, because
    this class is the boundary between analysis and a user's repository.
    """
    assert _public_methods(GitHubAppClient) == [
        "aclose",
        "get_repository",
        "get_tree",
        "list_branches",
        "list_repositories",
        "read_file",
    ]


def test_no_write_verb_appears_anywhere_in_the_module() -> None:
    """Catches a private helper that would let a write be added quietly."""
    source = Path("backend/github/client.py").read_text().lower()

    for verb in ("self._http.put", "self._http.patch", "self._http.delete"):
        assert verb not in source, f"{verb} present in a read-only client"

    # One POST is legitimate and only one: minting an installation token.
    assert source.count("self._http.post") == 1


def test_an_unauthorized_repository_is_refused() -> None:
    error = RepositoryNotAuthorized("acme/not-granted")

    assert error.status_code == 403
    assert "not in this installation" in str(error)


def test_absent_configuration_yields_not_configured_rather_than_a_crash() -> None:
    from backend.github.client import build_github_client

    settings = Settings(CONTINUITY_ENV=Environment.TEST, _env_file=None)

    with pytest.raises(IntegrationNotConfigured) as excinfo:
        build_github_client(settings, installation_id=1)

    assert excinfo.value.detail["integration"] == "GitHub App"
    assert "GITHUB_APP_ID" in excinfo.value.detail["reason"]


def test_tokens_are_never_persisted_to_the_database() -> None:
    """§3: installation tokens live in memory only."""
    source = Path("backend/github/client.py").read_text()

    assert "session.add" not in source
    assert "AsyncSession" not in source


def test_the_token_is_never_placed_in_an_error_message() -> None:
    """An exception carrying a token would leak it into logs and responses."""
    source = Path("backend/github/client.py").read_text()

    for line in source.splitlines():
        if "raise GitHubError" in line or "GitHubError(f" in line:
            assert "token" not in line.lower() or "installation token" in line.lower()


def test_reading_a_file_guards_the_path_before_the_request() -> None:
    """An excluded path must cost no network call and must not be fetchable."""
    source = inspect.getsource(GitHubAppClient.read_file)

    guard_index = source.index("guard_readable(path)")
    request_index = source.index("self._get(")
    assert guard_index < request_index, "the path guard must run before the request"
