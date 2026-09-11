"""C7-01: every write happens somewhere the user's code is not.

Migration agents never touch the user's default branch or working tree. Each run
gets an isolated git worktree checked out at a pinned source commit, and
everything — every file write, every command, every test — happens inside it.

Three properties, all of them structural rather than careful:

* **The source is untouched.** A worktree checks out into a separate directory;
  the branch does not move and no tracked file is rewritten. Asserted by hashing
  the source tree before and after.
* **Writes cannot escape.** `write_file` resolves the target and refuses
  anything that does not land under the workspace root — including via a symlink
  planted inside it, which is the case a string-prefix check passes.
* **Cleanup is deterministic.** Workspaces are removed on success, on failure,
  and — for the case nobody can catch in a `finally` — by a sweep at startup.
  A crashed process leaves a directory behind; the next start reclaims it.

Git runs through `ExecutionProvider` like everything else. `git worktree` is
allowlisted two levels deep (`add`, `remove`, `prune`, `list`) rather than `git`
being opened up: none of those reach the network, and the paths they receive are
constructed here, never by a model.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend.observability.logging import get_logger
from backend.shared.errors import ContinuityError
from backend.shared.execution import (
    CommandResult,
    CommandSpec,
    DevelopmentIsolatedExecutor,
    ExecutionAudit,
)

logger = get_logger(__name__)

#: Prefix every Continuity workspace directory carries. The sweep uses it to
#: tell its own leftovers from whatever else shares the parent directory —
#: deleting a directory we did not create would be unforgivable.
WORKSPACE_PREFIX: Final = "continuity-ws-"

#: Never read or hash anything under these when comparing source trees.
_HASH_EXCLUDE: Final = frozenset({".git", "__pycache__", ".pytest_cache", "node_modules"})

GIT_TIMEOUT_SECONDS: Final = 120


class WorkspaceError(ContinuityError):
    code = "workspace_error"
    status_code = 500
    message = "The migration workspace could not be prepared."


class WorkspaceWriteRejected(ContinuityError):
    """A write that would have landed outside the workspace.

    Not an error to recover from — it means something tried to modify the user's
    machine from inside a migration, and the attempt is recorded.
    """

    code = "workspace_write_rejected"
    status_code = 403
    message = "The write target is outside the migration workspace."


@dataclass(slots=True)
class WorkspaceRecord:
    """What a workspace did, for the attempt record and the pull request.

    `02_ARCHITECTURE.md` §12 lists exactly these.
    """

    source_commit: str
    target_branch: str
    files_changed: list[str] = field(default_factory=list)
    commands_executed: list[dict[str, object]] = field(default_factory=list)
    rejected_writes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "source_commit": self.source_commit,
            "target_branch": self.target_branch,
            "files_changed": sorted(self.files_changed),
            "commands_executed": list(self.commands_executed),
            "rejected_writes": list(self.rejected_writes),
        }


class MigrationWorkspace:
    """An isolated checkout. All migration writes go through this object."""

    def __init__(
        self,
        root: Path,
        *,
        source_commit: str,
        target_branch: str,
        executor: DevelopmentIsolatedExecutor,
    ) -> None:
        self._root = root.resolve()
        self._executor = executor
        self.record = WorkspaceRecord(
            source_commit=source_commit, target_branch=target_branch
        )

    @property
    def root(self) -> Path:
        return self._root

    # -- confined file access ---------------------------------------------

    def resolve(self, relative_path: str) -> Path:
        """Resolve a path inside the workspace, or refuse.

        `resolve()` follows symlinks deliberately: a link planted inside the
        workspace that points at the user's home directory resolves there, and
        a prefix comparison on the unresolved path would happily accept it.

        The parent is resolved rather than the target, because the target may
        not exist yet — that is the normal case for a new file.
        """
        if os.path.isabs(relative_path):
            raise WorkspaceWriteRejected(
                f"{relative_path} is absolute; workspace paths are repository-relative"
            )

        candidate = (self._root / relative_path).parent.resolve() / Path(
            relative_path
        ).name

        if candidate != self._root and self._root not in candidate.parents:
            raise WorkspaceWriteRejected(
                f"{relative_path} resolves to {candidate}, outside {self._root}"
            )
        return candidate

    def read_file(self, relative_path: str) -> str:
        return self.resolve(relative_path).read_text()

    def exists(self, relative_path: str) -> bool:
        return self.resolve(relative_path).exists()

    def write_file(self, relative_path: str, content: str) -> Path:
        """Write inside the workspace, recording the change."""
        try:
            target = self.resolve(relative_path)
        except WorkspaceWriteRejected as rejection:
            self.record.rejected_writes.append(relative_path)
            logger.warning(
                "continuity.workspace_write_rejected",
                extra={"path": relative_path, "workspace": str(self._root)},
            )
            raise rejection

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if relative_path not in self.record.files_changed:
            self.record.files_changed.append(relative_path)
        return target

    def delete_file(self, relative_path: str) -> None:
        """Remove a file, recording it.

        Deleting is permitted here and forbidden higher up: the Migration
        Engineer may not delete a test (C7-02), and that rule belongs where the
        intent is known rather than in the filesystem primitive.
        """
        target = self.resolve(relative_path)
        if target.exists():
            target.unlink()
            if relative_path not in self.record.files_changed:
                self.record.files_changed.append(relative_path)

    # -- git ---------------------------------------------------------------

    async def run(self, argv: list[str], *, timeout_seconds: int = GIT_TIMEOUT_SECONDS) -> CommandResult:
        """Run a command inside the workspace, recording it."""
        result = await self._executor.run(
            CommandSpec(
                argv=argv,
                cwd=self._root,
                timeout_seconds=timeout_seconds,
                env=workspace_env(self._root),
                max_output_bytes=1_048_576,
            )
        )
        self.record.commands_executed.append(
            {
                "argv": list(argv),
                "exit_code": result.exit_code,
                "status": result.status.value,
                "duration_ms": result.duration_ms,
            }
        )
        return result

    async def diff(self) -> str:
        """The patch this workspace represents, from git rather than from memory.

        Asking git means the diff reflects what is on disk, including a change
        made by a command rather than by `write_file`. A diff assembled from the
        writes we happened to record would quietly omit those.
        """
        await self.run(["git", "add", "-A"])
        result = await self.run(["git", "diff", "--cached"])
        return result.stdout

    async def changed_files(self) -> list[str]:
        """Files git says changed, which is the authoritative answer."""
        await self.run(["git", "add", "-A"])
        result = await self.run(["git", "diff", "--cached", "--name-only"])
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def workspace_env(root: Path) -> dict[str, str]:
    """The environment a workspace command gets.

    Only allowlisted names, and nothing of Continuity's. `HOME` points into the
    workspace so a tool that writes a dotfile does it here rather than in the
    user's home directory.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "TMPDIR": str(root),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }


class WorkspaceManager:
    """Creates, cleans up, and reclaims migration workspaces."""

    def __init__(
        self,
        source_repo: Path,
        *,
        workspace_root: Path,
        audit: ExecutionAudit,
    ) -> None:
        self._source = Path(source_repo).resolve()
        self._workspace_root = Path(workspace_root).resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._audit = audit
        # Rooted at the source repository, because `git worktree add` must run
        # there. Its own executor, so a workspace command can never be handed a
        # cwd inside the user's checkout by accident.
        self._source_executor = DevelopmentIsolatedExecutor(self._source, audit=audit)

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def head_commit(self) -> str:
        result = await self._source_executor.run(
            CommandSpec(
                argv=["git", "rev-parse", "HEAD"],
                cwd=self._source,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                env=workspace_env(self._source),
                max_output_bytes=65_536,
            )
        )
        commit = result.stdout.strip()
        if not result.ok or not commit:
            raise WorkspaceError(
                f"could not read HEAD of {self._source}: {result.stderr.strip()[:200]}"
            )
        return commit

    @asynccontextmanager
    async def open(
        self,
        *,
        run_id: uuid.UUID,
        source_commit: str | None = None,
        target_branch: str,
    ) -> AsyncIterator[MigrationWorkspace]:
        """An isolated worktree, removed however this block exits.

        The context manager is the point: cleanup on success and cleanup on
        failure are the same line of code, so there is no path that returns
        early and leaves a checkout of the user's repository lying around.
        """
        commit = source_commit or await self.head_commit()
        path = self._workspace_root / f"{WORKSPACE_PREFIX}{run_id}"

        await self._create_worktree(path, commit)
        workspace = MigrationWorkspace(
            path,
            source_commit=commit,
            target_branch=target_branch,
            executor=DevelopmentIsolatedExecutor(path, audit=self._audit),
        )
        logger.info(
            "continuity.workspace_opened",
            extra={"run_id": str(run_id), "commit": commit, "path": str(path)},
        )
        try:
            yield workspace
        finally:
            await self.cleanup(path)

    async def _create_worktree(self, path: Path, commit: str) -> None:
        if await asyncio.to_thread(path.exists):
            # A leftover from a crashed run with the same id. Reclaim it rather
            # than failing, and rather than checking out on top of it.
            await self.cleanup(path)

        result = await self._source_executor.run(
            CommandSpec(
                # --detach: the workspace is not a branch and must never be
                # mistaken for one. Delivery creates the branch through the
                # GitHub App, not here.
                argv=["git", "worktree", "add", "--detach", str(path), commit],
                cwd=self._source,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                env=workspace_env(self._source),
                max_output_bytes=1_048_576,
            )
        )
        if not result.ok:
            raise WorkspaceError(
                f"git worktree add failed for {commit}: {result.stderr.strip()[:400]}"
            )

    async def cleanup(self, path: Path) -> None:
        """Remove one workspace, whatever state it is in.

        `git worktree remove` first, because it also clears the source repo's
        bookkeeping. `rmtree` after, because a half-created worktree is not a
        worktree git will admit to and would otherwise stay on disk forever.
        """
        if not str(path).startswith(str(self._workspace_root)):
            raise WorkspaceError(f"refusing to clean up {path}: outside the workspace root")

        await self._source_executor.run(
            CommandSpec(
                argv=["git", "worktree", "remove", "--force", str(path)],
                cwd=self._source,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                env=workspace_env(self._source),
                max_output_bytes=1_048_576,
            )
        )
        if await asyncio.to_thread(path.exists):
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)

        await self._source_executor.run(
            CommandSpec(
                argv=["git", "worktree", "prune"],
                cwd=self._source,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                env=workspace_env(self._source),
                max_output_bytes=65_536,
            )
        )
        logger.info("continuity.workspace_cleaned", extra={"path": str(path)})

    async def sweep_orphans(self, *, max_age_seconds: int = 0) -> list[str]:
        """Reclaim workspaces a crashed process left behind.

        The case a `finally` cannot cover: the process died. Run at startup, so
        a crash costs disk until the next start rather than forever.

        Only directories carrying `WORKSPACE_PREFIX` are touched — deleting
        something Continuity did not create would be far worse than leaking a
        directory.
        """
        swept: list[str] = []
        now = time.time()

        # Scanned in a thread: the sweep runs at startup, and a slow or
        # network-mounted workspace root would otherwise block the event loop
        # before the application has served anything.
        candidates = await asyncio.to_thread(self._orphan_candidates, now, max_age_seconds)

        for candidate in candidates:
            await self.cleanup(candidate)
            swept.append(candidate.name)

        if swept:
            logger.warning(
                "continuity.workspace_orphans_swept",
                extra={"count": len(swept), "root": str(self._workspace_root)},
            )
        return swept

    def _orphan_candidates(self, now: float, max_age_seconds: int) -> list[Path]:
        found = []
        for candidate in sorted(self._workspace_root.iterdir()):
            if not candidate.is_dir() or not candidate.name.startswith(WORKSPACE_PREFIX):
                continue
            if max_age_seconds and (now - candidate.stat().st_mtime) < max_age_seconds:
                continue
            found.append(candidate)
        return found


# ---------------------------------------------------------------------------
# Source-tree verification
# ---------------------------------------------------------------------------


def tree_hash(root: Path, *, exclude: Iterable[str] = _HASH_EXCLUDE) -> str:
    """A recursive hash of a directory's content and layout.

    Content *and* layout: hashing bytes alone would not notice a file moved, and
    "the user's tree is unchanged" has to mean unchanged, not merely
    equal-in-aggregate.
    """
    excluded = set(exclude)
    digest = hashlib.sha256()

    for path in sorted(root.rglob("*")):
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            digest.update(f"d:{relative}\n".encode())
        elif path.is_symlink():
            digest.update(f"l:{relative}:{os.readlink(path)}\n".encode())
        elif path.is_file():
            digest.update(f"f:{relative}:".encode())
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
            digest.update(b"\n")

    return digest.hexdigest()
