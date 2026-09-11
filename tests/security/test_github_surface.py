"""C3-02 / C8-03 acceptance: what the GitHub client can and cannot do.

`03_SECURITY_ACCESS.md` §3 lists operations Continuity must never perform. The
guarantee is structural rather than conditional: there is no method to call, so
no flag, prompt, or agent can reach one.

**Amended in Phase 8.** This file originally asserted the client was read-only,
which it was until delivery existed. C8-03 requires creating a branch, a commit,
and a pull request, so the pinned surface now includes exactly those — and
nothing else. The forbidden list is unchanged and is what actually matters:
force-push, merge, delete, history rewrite, and writes to an arbitrary ref are
still absent, and still absent by construction rather than by refusal.

Widening this list is meant to be uncomfortable. It is the boundary between
analysis and someone's repository.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.github.client import GitHubAppClient, RepositoryNotAuthorized
from backend.shared.config import Environment, Settings
from backend.shared.errors import IntegrationNotConfigured

#: Operation shapes that must never exist on this client. `commit` and
#: `create_branch` left this list in Phase 8 because delivery needs them;
#: everything here is something no migration should ever be able to do.
FORBIDDEN_FRAGMENTS = (
    "force",
    "push",
    "merge",
    "rewrite",
    "delete",
    "reset",
    "squash",
    "rebase",
)

#: The writes delivery needs, pinned one by one. Adding to this list should
#: require the same argument C8-03 had to make.
ALLOWED_WRITES = (
    "create_blob",
    "create_branch",
    "create_commit",
    "create_pull_request",
    "create_tree",
    "update_branch",
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
    assert _public_methods(GitHubAppClient) == sorted(
        [
            "aclose",
            "get_pull_request",
            "get_ref",
            "get_repository",
            "get_tree",
            "list_branches",
            "list_repositories",
            "read_file",
            *ALLOWED_WRITES,
        ]
    )


def test_the_write_surface_is_branch_and_pull_request_only() -> None:
    """C8-03: exactly the six writes delivery needs.

    `update_branch` is the one that could be dangerous, and it is safe for a
    specific reason: it sends no `force`, so GitHub refuses a non-fast-forward
    update. A failed migration cannot overwrite a reviewer's commit.
    """
    methods = set(_public_methods(GitHubAppClient))
    writes = {name for name in methods if name.startswith(("create_", "update_"))}

    assert writes == set(ALLOWED_WRITES)

    source = Path("backend/github/client.py").read_text()
    assert '"force"' not in source
    assert "force=True" not in source


def test_no_write_verb_appears_anywhere_in_the_module() -> None:
    """Catches a private helper that would let a write be added quietly."""
    source = Path("backend/github/client.py").read_text().lower()

    # PUT and DELETE are absent, and stay absent: nothing Continuity does
    # replaces or removes a resource.
    for verb in ("self._http.put", "self._http.delete"):
        assert verb not in source, f"{verb} present: Continuity never {verb[11:]}s"

    # POST twice: minting an installation token, and `_post`. PATCH once, in
    # `_patch`. Every write goes through one of those two helpers, which is what
    # makes them greppable in one place.
    assert source.count("self._http.post") == 2
    assert source.count("self._http.patch") == 1


def test_updating_a_branch_uses_the_endpoint_github_actually_has() -> None:
    """Regression: this was a POST, and a fake API was happy to accept it.

    GitHub's git-data API distinguishes the two verbs — POST to `/git/refs`
    creates a ref, PATCH to `/git/refs/{ref}` updates one. POSTing to the update
    path is not an endpoint at all, so delivery would have created a branch and
    a commit and then silently failed to attach one to the other, against real
    GitHub, while every mocked test passed.
    """
    import ast
    import inspect
    import textwrap

    from backend.github.client import GitHubAppClient

    tree = ast.parse(textwrap.dedent(inspect.getsource(GitHubAppClient.update_branch)))
    function = tree.body[0]
    assert isinstance(function, ast.AsyncFunctionDef)

    # The docstring explains that no `force` is sent, so a text search finds the
    # word and proves nothing. Strip it and look at the code.
    body = function.body[1:] if ast.get_docstring(function) else function.body
    code = "\n".join(ast.unparse(node) for node in body)

    assert "self._patch(" in code
    assert "self._post(" not in code
    assert "force" not in code


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
