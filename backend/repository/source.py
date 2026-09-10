"""Repository access, boundary-enforced.

`RepositorySource` is the single interface through which Continuity reads code.
Two implementations exist: `LocalRepositoryAdapter` (development and tests) and
the GitHub App source (`backend/github/`). Both enforce the same two rules, and
a shared conformance suite runs against both so they cannot drift apart:

* **Every path is confined to the repository root.** `..`, absolute paths, and
  symlinks pointing outside are rejected with a typed error, not silently
  resolved.
* **Excluded paths are never opened.** `read_file` refuses them before any I/O,
  so a `.env` cannot be read even by a caller that asks for it directly.

The boundary check lives here rather than in each caller because a caller that
forgets it is exactly the bug this prevents.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from backend.security.secret_filter import classify_path, is_excluded_path
from backend.shared.errors import ContinuityError


class RepositoryBoundaryViolation(ContinuityError):
    """A path escaped the repository root.

    Always a bug or an attack, never ordinary use — so it raises rather than
    returning empty, and the audit trail records the attempted path.
    """

    code = "repository_boundary_violation"
    status_code = 403
    message = "That path is outside the authorized repository."

    def __init__(self, attempted: str, reason: str) -> None:
        super().__init__(
            f"Refused path outside the repository boundary: {attempted!r} ({reason}).",
            attempted=attempted,
            reason=reason,
        )


class ExcludedPathRequested(ContinuityError):
    """A caller asked for a path the secret filter forbids."""

    code = "excluded_path_requested"
    status_code = 403
    message = "That path is excluded from analysis."

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            f"Refused excluded path: {path!r} ({reason}).", path=path, reason=reason
        )


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    """A file the source is willing to expose."""

    path: str  # repository-relative, POSIX
    size_bytes: int
    is_binary_suffix: bool = False


@runtime_checkable
class RepositorySource(Protocol):
    """Read-only access to one repository at one commit."""

    @property
    def identifier(self) -> str: ...

    def list_files(self) -> Iterator[RepositoryFile]:
        """Every readable file, already exclusion-filtered."""
        ...

    def read_file(self, path: str) -> str:
        """Text of one file. Raises if excluded or outside the boundary."""
        ...

    def file_size(self, path: str) -> int:
        """Size without reading the content, so large files can be skipped."""
        ...

    def skipped_paths(self) -> list[tuple[str, str]]:
        """`(path, reason)` for everything excluded during the last listing.

        Required so a scan can report what it refused to open. A source that
        prunes silently would make the secret-exclusion count read zero, which
        understates a control that is working.
        """
        ...


def normalize_relative_path(path: str) -> str:
    """Reject anything that is not a plain repository-relative path.

    Rejects absolute paths, `..` segments, and Windows drive letters *before*
    resolution rather than after, because a check that resolves first has
    already been told where to look.
    """
    if not path or path in (".", "/"):
        raise RepositoryBoundaryViolation(path, "empty or root path")

    candidate = path.replace("\\", "/")

    if candidate.startswith("/"):
        raise RepositoryBoundaryViolation(path, "absolute path")
    if len(candidate) > 1 and candidate[1] == ":":
        raise RepositoryBoundaryViolation(path, "drive-qualified path")
    if candidate.startswith("~"):
        raise RepositoryBoundaryViolation(path, "home-relative path")

    pure = PurePosixPath(candidate)
    if any(part == ".." for part in pure.parts):
        raise RepositoryBoundaryViolation(path, "parent traversal")
    if any(part.startswith("\x00") or "\x00" in part for part in pure.parts):
        raise RepositoryBoundaryViolation(path, "null byte in path")

    return pure.as_posix()


def resolve_within(root: Path, relative_path: str) -> Path:
    """Resolve a relative path and prove the result is still inside `root`.

    Both sides are fully resolved before comparison, which is what catches a
    symlink whose *name* is innocent but whose target is not.
    """
    normalized = normalize_relative_path(relative_path)
    resolved_root = root.resolve()
    candidate = (resolved_root / normalized).resolve()

    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise RepositoryBoundaryViolation(relative_path, "resolves outside the repository root")

    return candidate


def guard_readable(
    relative_path: str, *, extra_secret_patterns: tuple[str, ...] = ()
) -> str:
    """Normalize, then refuse excluded paths. Call before any read."""
    normalized = normalize_relative_path(relative_path)
    verdict = classify_path(normalized, extra_secret_patterns=extra_secret_patterns)
    if verdict.excluded:
        raise ExcludedPathRequested(normalized, verdict.detail or verdict.reason.value)
    return normalized


def readable(relative_path: str) -> bool:
    """Non-raising form, for filtering a listing."""
    try:
        normalized = normalize_relative_path(relative_path)
    except RepositoryBoundaryViolation:
        return False
    return not is_excluded_path(normalized)
