"""C3-03 acceptance: both repository sources behave identically.

One suite, parametrized over `LocalRepositoryAdapter` and
`GitHubRepositorySource`. This exists because a boundary or exclusion rule that
holds locally and lapses over GitHub would be invisible until it mattered —
the local adapter is what everything is developed against, and the GitHub source
is what runs against real customer code.

The GitHub source is driven by a fake client rather than the network, so this
runs without blocker B-02 being resolved. The fake implements only the four
methods the source actually calls, so it cannot accidentally provide behaviour
the real client does not have.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.github.repository_source import GitHubRepositorySource, RepositoryNotLoaded
from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.repository.source import (
    ExcludedPathRequested,
    RepositoryBoundaryViolation,
)

# A repository shaped to exercise every rule: readable source, a secret file, a
# dependency directory, a binary, and a .gitignore naming a project-specific
# secret our static list could not predict.
TREE: dict[str, str] = {
    ".gitignore": "dist/\n*.log\ndeploy/live-credentials\n",
    "app/main.py": 'import httpx\n\n\ndef go():\n    return httpx.get("/v1/x")\n',
    "app/util.py": "VALUE = 1\n",
    "pyproject.toml": '[project]\nname = "x"\ndependencies = ["httpx"]\n',
    ".env": "SECRET=value\n",
    "config/key.pem": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----\n",
    "deploy/live-credentials": "user=admin\npassword=hunter2\n",
    "node_modules/dep/index.js": "module.exports = 1;\n",
    "assets/logo.png": "\x89PNG\r\n",
}


class FakeGitHubClient:
    """The minimum surface `GitHubRepositorySource` uses. Nothing more."""

    def __init__(self, tree: dict[str, str]) -> None:
        self._tree = tree
        self.read_paths: list[str] = []

    async def get_tree(self, owner: str, name: str, ref: str) -> list[dict[str, Any]]:
        return [
            {"path": path, "type": "blob", "size": len(content.encode())}
            for path, content in sorted(self._tree.items())
        ]

    async def read_file(self, owner: str, name: str, path: str, ref: str) -> str:
        self.read_paths.append(path)
        return self._tree[path]


@pytest.fixture
def local_source(tmp_path: Path) -> LocalRepositoryAdapter:
    for path, content in TREE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return LocalRepositoryAdapter(tmp_path)


@pytest.fixture
async def github_source() -> GitHubRepositorySource:
    client = FakeGitHubClient(TREE)
    source = GitHubRepositorySource(client, "acme", "commerce-api", "main")  # type: ignore[arg-type]
    return await source.load()


@pytest.fixture(params=["local", "github"])
async def source(request: pytest.FixtureRequest, tmp_path: Path):
    """The same suite, once per implementation."""
    if request.param == "local":
        for path, content in TREE.items():
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return LocalRepositoryAdapter(tmp_path)

    client = FakeGitHubClient(TREE)
    return await GitHubRepositorySource(client, "acme", "commerce-api", "main").load()  # type: ignore[arg-type]


# --- Listing -------------------------------------------------------------


async def test_readable_files_are_listed(source) -> None:
    listed = {file.path for file in source.list_files()}

    assert "app/main.py" in listed
    assert "pyproject.toml" in listed


async def test_secrets_are_never_listed(source) -> None:
    listed = {file.path for file in source.list_files()}

    assert ".env" not in listed
    assert "config/key.pem" not in listed


async def test_repository_declared_secrets_are_never_listed(source) -> None:
    """The `.gitignore` rule our static patterns could not have guessed."""
    listed = {file.path for file in source.list_files()}

    assert "deploy/live-credentials" not in listed


async def test_dependency_and_binary_paths_are_not_listed(source) -> None:
    listed = {file.path for file in source.list_files()}

    assert "node_modules/dep/index.js" not in listed
    assert "assets/logo.png" not in listed


async def test_skipped_paths_are_reported(source) -> None:
    list(source.list_files())
    skipped = {path for path, _ in source.skipped_paths()}

    assert ".env" in skipped
    assert "deploy/live-credentials" in skipped


# --- Reading -------------------------------------------------------------


async def test_a_readable_file_can_be_read(source) -> None:
    assert "httpx" in source.read_file("app/main.py")


@pytest.mark.parametrize(
    "path", [".env", "config/key.pem", "deploy/live-credentials", "assets/logo.png"]
)
async def test_excluded_paths_are_refused_on_direct_request(source, path: str) -> None:
    """Filtering the listing is not enough; a direct ask must fail too."""
    with pytest.raises(ExcludedPathRequested):
        source.read_file(path)


@pytest.mark.parametrize(
    "path", ["../escape", "/etc/passwd", "~/.ssh/id_rsa", "app/../../etc/passwd", ".."]
)
async def test_boundary_escapes_are_refused(source, path: str) -> None:
    with pytest.raises(RepositoryBoundaryViolation):
        source.read_file(path)


async def test_file_size_is_available_without_reading(source) -> None:
    assert source.file_size("app/util.py") > 0


async def test_identifier_names_the_source_kind(source) -> None:
    assert source.identifier.startswith(("local:", "github:"))


# --- GitHub-specific guarantees ------------------------------------------


async def test_the_github_source_never_fetches_an_excluded_file() -> None:
    """Excluded paths cost no API call and are never transferred."""
    client = FakeGitHubClient(TREE)
    await GitHubRepositorySource(client, "acme", "commerce-api", "main").load()  # type: ignore[arg-type]

    for forbidden in (".env", "config/key.pem", "deploy/live-credentials"):
        assert forbidden not in client.read_paths, f"{forbidden} was fetched from GitHub"


async def test_the_github_source_fetches_gitignore_before_deciding() -> None:
    """Its rules govern the rest of the walk, so it must come first."""
    client = FakeGitHubClient(TREE)
    await GitHubRepositorySource(client, "acme", "commerce-api", "main").load()  # type: ignore[arg-type]

    assert client.read_paths[0] == ".gitignore"


async def test_using_the_github_source_before_loading_fails_loudly() -> None:
    """Silently returning nothing would look like an empty repository."""
    source = GitHubRepositorySource(FakeGitHubClient(TREE), "a", "b", "main")  # type: ignore[arg-type]

    with pytest.raises(RepositoryNotLoaded):
        list(source.list_files())


# --- Both sources produce the same index ---------------------------------


async def test_both_sources_produce_the_same_index(
    local_source: LocalRepositoryAdapter, github_source: GitHubRepositorySource
) -> None:
    """The strongest conformance claim: identical input, identical analysis."""
    from backend.repository.indexer import build_index

    local_index = build_index(local_source)
    github_index = build_index(github_source)

    assert set(local_index.files) == set(github_index.files)
    assert local_index.summary()["secret_paths_excluded"] == (
        github_index.summary()["secret_paths_excluded"]
    )
    assert local_index.summary()["symbols"] == github_index.summary()["symbols"]
    assert {d.name for d in local_index.dependencies} == {
        d.name for d in github_index.dependencies
    }
