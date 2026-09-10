"""A `RepositorySource` backed by the GitHub App.

The point of this module is conformance: the indexer, retrieval, and the scan
worker must behave identically whether code comes from a local checkout or from
GitHub. A shared conformance suite runs against both
(`tests/integration/test_repository_source_conformance.py`), so a boundary or
exclusion rule cannot hold in one and lapse in the other.

**Why it loads eagerly.** `RepositorySource` is synchronous, and the GitHub API
is not. Rather than make the whole indexing pipeline async for one source, this
fetches the tree and the contents it will need in one `load()` step, then serves
them synchronously. That is not a workaround — the repository has to be fetched
regardless, and the exclusion filter and size limit bound what is held in memory
to exactly what the indexer would have read anyway.

Excluded paths are filtered from the tree *before* any content request, so a
`.env` costs no API call and, more to the point, is never fetched at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from backend.github.client import GitHubAppClient
from backend.observability.logging import get_logger
from backend.repository.source import (
    RepositoryFile,
    guard_readable,
    normalize_relative_path,
)
from backend.security.secret_filter import classify_path, secret_patterns_from_gitignore
from backend.shared.errors import ContinuityError

logger = get_logger(__name__)

#: Matches the indexer's own limit; a larger file would be recorded and skipped
#: anyway, so fetching it would be pure cost.
MAX_FETCH_BYTES = 400_000


class RepositoryNotLoaded(ContinuityError):
    """The source was used before `load()` completed."""

    code = "repository_not_loaded"
    status_code = 409
    message = "This repository source has not been loaded yet."


class GitHubRepositorySource:
    """Read-only `RepositorySource` over one repository at one ref."""

    def __init__(self, client: GitHubAppClient, owner: str, name: str, ref: str) -> None:
        self._client = client
        self._owner = owner
        self._name = name
        self._ref = ref
        self._files: dict[str, RepositoryFile] | None = None
        self._contents: dict[str, str] = {}
        self._skipped: list[tuple[str, str]] = []
        self._extra_secret_patterns: tuple[str, ...] = ()

    @property
    def identifier(self) -> str:
        return f"github:{self._owner}/{self._name}@{self._ref}"

    async def load(self) -> GitHubRepositorySource:
        """Fetch the tree and the readable file contents.

        `.gitignore` is fetched first, because its secret rules decide what the
        rest of the walk is allowed to fetch.
        """
        tree = await self._client.get_tree(self._owner, self._name, self._ref)

        self._extra_secret_patterns = await self._load_gitignore_patterns(tree)

        files: dict[str, RepositoryFile] = {}
        self._skipped.clear()

        for entry in tree:
            path = entry.get("path", "")
            if not path:
                continue

            try:
                normalized = normalize_relative_path(path)
            except ContinuityError:
                # A tree entry that will not normalize is malformed or hostile.
                self._skipped.append((path, "path failed normalization"))
                continue

            verdict = classify_path(
                normalized, extra_secret_patterns=self._extra_secret_patterns
            )
            if verdict.excluded:
                self._skipped.append((normalized, verdict.detail or verdict.reason.value))
                continue

            size = int(entry.get("size", 0) or 0)
            files[normalized] = RepositoryFile(path=normalized, size_bytes=size)

            if size <= MAX_FETCH_BYTES:
                self._contents[normalized] = await self._client.read_file(
                    self._owner, self._name, normalized, self._ref
                )

        self._files = files
        logger.info(
            "continuity.github_repository_loaded",
            extra={
                "repository": f"{self._owner}/{self._name}",
                "ref": self._ref,
                "files": len(files),
                "skipped": len(self._skipped),
            },
        )
        return self

    async def _load_gitignore_patterns(self, tree: list[dict[str, Any]]) -> tuple[str, ...]:
        if not any(entry.get("path") == ".gitignore" for entry in tree):
            return ()
        try:
            content = await self._client.read_file(
                self._owner, self._name, ".gitignore", self._ref
            )
        except ContinuityError:
            return ()
        return tuple(secret_patterns_from_gitignore(content))

    # --- RepositorySource ---

    def _require_loaded(self) -> dict[str, RepositoryFile]:
        if self._files is None:
            raise RepositoryNotLoaded(
                f"Call await load() before reading {self.identifier}."
            )
        return self._files

    def list_files(self) -> Iterator[RepositoryFile]:
        yield from (self._require_loaded()[path] for path in sorted(self._require_loaded()))

    def skipped_paths(self) -> list[tuple[str, str]]:
        return list(self._skipped)

    def file_size(self, path: str) -> int:
        normalized = guard_readable(path, extra_secret_patterns=self._extra_secret_patterns)
        entry = self._require_loaded().get(normalized)
        if entry is None:
            raise RepositoryNotLoaded(f"{normalized!r} is not part of this repository.")
        return entry.size_bytes

    def read_file(self, path: str) -> str:
        """Content of one file, boundary- and exclusion-checked.

        The guard runs even though `load()` already filtered, because a caller
        can ask for any path and the answer must not depend on how the file was
        stored.
        """
        normalized = guard_readable(path, extra_secret_patterns=self._extra_secret_patterns)
        self._require_loaded()
        return self._contents.get(normalized, "")

    @property
    def extra_secret_patterns(self) -> tuple[str, ...]:
        return self._extra_secret_patterns
