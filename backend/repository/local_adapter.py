"""Local filesystem repository source — DEVELOPMENT AND TESTS ONLY.

Named so it cannot be mistaken for GitHub access. It exists so ingestion,
indexing, and retrieval can be built and tested before a GitHub App is
registered (`STATUS.md` blocker B-02), and it enforces exactly the same boundary
and exclusion rules as the GitHub source so behaviour does not diverge.

It is never presented to a user as repository access, and it never appears in a
code path that claims to be reading a connected repository.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from backend.repository.source import (
    RepositoryBoundaryViolation,
    RepositoryFile,
    guard_readable,
    readable,
    resolve_within,
)
from backend.security.secret_filter import classify_path, secret_patterns_from_gitignore

#: Files larger than this are indexed by name and size only. A 5 MB generated
#: bundle costs budget and teaches nothing; the limit is checked with `stat`, so
#: an oversized file is never read to discover it is oversized.
MAX_FILE_BYTES = 1_000_000


def _load_gitignore_secret_patterns(root: Path) -> tuple[str, ...]:
    """Read the repository's `.gitignore`, if present, for secret rules."""
    candidate = root / ".gitignore"
    try:
        if candidate.is_file():
            return tuple(secret_patterns_from_gitignore(candidate.read_text(errors="replace")))
    except OSError:
        pass
    return ()


class LocalRepositoryAdapter:
    """A `RepositorySource` backed by a directory on disk."""

    def __init__(self, root: Path, *, max_file_bytes: int = MAX_FILE_BYTES) -> None:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise RepositoryBoundaryViolation(str(root), "not a directory")
        self._root = resolved
        self._max_file_bytes = max_file_bytes
        # Pruned paths are recorded rather than silently dropped. The walk skips
        # excluded directories for speed, so without this the indexer would
        # never see them and the scan summary would report zero secrets
        # excluded — understating a security control that is actually working.
        self._skipped: list[tuple[str, str]] = []
        # The repository's own .gitignore often names secret files our static
        # list cannot predict — a team that ignores `deploy/live.conf` is
        # telling us it holds credentials (`03_SECURITY_ACCESS.md` §2).
        self._extra_secret_patterns = _load_gitignore_secret_patterns(resolved)

    @property
    def identifier(self) -> str:
        return f"local:{self._root.name}"

    @property
    def root(self) -> Path:
        return self._root

    def list_files(self) -> Iterator[RepositoryFile]:
        """Walk the tree, skipping excluded paths without opening them.

        Excluded *directories* are pruned rather than walked, so a large
        `node_modules` costs one check instead of a hundred thousand.
        """
        self._skipped.clear()
        yield from self._walk(self._root)

    def skipped_paths(self) -> list[tuple[str, str]]:
        """`(path, reason)` for everything pruned during the last walk."""
        return list(self._skipped)

    def _walk(self, directory: Path) -> Iterator[RepositoryFile]:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            return

        for entry in entries:
            relative = entry.relative_to(self._root).as_posix()

            if entry.is_symlink():
                # A symlink is followed only if its target stays inside the
                # repository. Silently skipping is right here: a link out of the
                # tree is usually a toolchain artifact, not an attack.
                try:
                    resolve_within(self._root, relative)
                except RepositoryBoundaryViolation:
                    continue

            verdict = classify_path(
                relative, extra_secret_patterns=self._extra_secret_patterns
            )
            if verdict.excluded:
                self._skipped.append((relative, verdict.detail or verdict.reason.value))
                continue

            if entry.is_dir():
                yield from self._walk(entry)
                continue

            if not entry.is_file():
                continue

            try:
                size = entry.stat().st_size
            except OSError:
                continue

            yield RepositoryFile(path=relative, size_bytes=size)

    @property
    def extra_secret_patterns(self) -> tuple[str, ...]:
        """Secret rules adopted from this repository's `.gitignore`."""
        return self._extra_secret_patterns

    def file_size(self, path: str) -> int:
        normalized = guard_readable(path, extra_secret_patterns=self._extra_secret_patterns)
        return resolve_within(self._root, normalized).stat().st_size

    def read_file(self, path: str) -> str:
        """Read one file, refusing excluded and out-of-boundary paths.

        Oversized files return empty rather than raising: the indexer wants to
        record that the file exists without paying to read it, and treating that
        as an error would make every large asset a scan failure.
        """
        normalized = guard_readable(path, extra_secret_patterns=self._extra_secret_patterns)
        target = resolve_within(self._root, normalized)

        if target.stat().st_size > self._max_file_bytes:
            return ""

        return target.read_text(encoding="utf-8", errors="replace")

    def exists(self, path: str) -> bool:
        return readable(path) and resolve_within(self._root, path).is_file()
