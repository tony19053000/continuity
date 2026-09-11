"""C7-01: isolating every write.

These tests create real git repositories and real worktrees. The property under
test is that the user's checkout is untouched, and that is only meaningful
against a real one — a mocked git would prove the mock was configured as
expected.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from backend.migrations.workspace import (
    WORKSPACE_PREFIX,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceWriteRejected,
    tree_hash,
    workspace_env,
)
from backend.observability.execution_audit import NullExecutionAudit
from backend.shared.execution import (
    ALLOWED_EXECUTABLES,
    CommandSpec,
    DevelopmentIsolatedExecutor,
    SubcommandNotAllowed,
)


@pytest.fixture
async def source_repo(tmp_path: Path) -> Path:
    """A small git repository standing in for the user's checkout."""
    repo = tmp_path / "userrepo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "payments.py").write_text("def charge():\n    return post('/v1/charges')\n")
    (repo / "tests" / "test_payments.py").write_text("def test_charge():\n    assert True\n")
    (repo / "README.md").write_text("# user project\n")

    # `git init` is not on the allowlist, and should not be: Continuity clones
    # nothing and initialises nothing in production. Fixture setup uses git
    # directly; everything under test goes through the executor.
    import subprocess

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607 - fixture setup, resolved from PATH
            cwd=repo,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        )

    run("init", "-b", "main")
    run("config", "user.email", "t@example.test")
    run("config", "user.name", "Test")
    run("add", "-A")
    run("commit", "-m", "initial")
    return repo


@pytest.fixture
def manager(source_repo: Path, tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(
        source_repo,
        workspace_root=tmp_path / "workspaces",
        audit=NullExecutionAudit(),
    )


# --- isolation -----------------------------------------------------------


async def test_the_users_tree_is_byte_identical_after_a_migration(
    manager: WorkspaceManager, source_repo: Path
) -> None:
    """C7-01 acceptance, by recursive hash.

    A migration rewrites a file, deletes another, and adds a third — inside the
    workspace. The user's checkout must be exactly as it was, content and
    layout both.
    """
    before = tree_hash(source_repo)

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        ws.write_file("app/payments.py", "def charge():\n    return post('/v2/charges')\n")
        ws.write_file("app/new_module.py", "X = 1\n")
        ws.delete_file("README.md")

    assert tree_hash(source_repo) == before
    assert (source_repo / "README.md").read_text() == "# user project\n"
    assert "v1/charges" in (source_repo / "app" / "payments.py").read_text()
    assert not (source_repo / "app" / "new_module.py").exists()


async def test_the_default_branch_does_not_move(
    manager: WorkspaceManager, source_repo: Path
) -> None:
    """A worktree is `--detach`ed, so nothing can be committed onto main by accident."""
    executor = DevelopmentIsolatedExecutor(source_repo, audit=NullExecutionAudit())

    async def head() -> str:
        result = await executor.run(
            CommandSpec(
                argv=["git", "rev-parse", "main"],
                cwd=source_repo,
                timeout_seconds=60,
                env=workspace_env(source_repo),
                max_output_bytes=65_536,
            )
        )
        return result.stdout.strip()

    before = await head()

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        ws.write_file("app/payments.py", "changed\n")
        await ws.run(["git", "add", "-A"])

    assert await head() == before


async def test_the_source_working_tree_stays_clean(
    manager: WorkspaceManager, source_repo: Path
) -> None:
    """`git status` in the user's checkout must show nothing.

    The hash proves the files are unchanged; this proves git agrees, which is
    what the user would actually look at.
    """
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        ws.write_file("app/payments.py", "changed\n")
        await ws.run(["git", "add", "-A"])

    executor = DevelopmentIsolatedExecutor(source_repo, audit=NullExecutionAudit())
    result = await executor.run(
        CommandSpec(
            argv=["git", "status", "--porcelain"],
            cwd=source_repo,
            timeout_seconds=60,
            env=workspace_env(source_repo),
            max_output_bytes=65_536,
        )
    )

    assert result.stdout.strip() == ""


async def test_the_workspace_starts_as_a_copy_of_the_pinned_commit(
    manager: WorkspaceManager, source_repo: Path
) -> None:
    commit = await manager.head_commit()

    async with manager.open(
        run_id=uuid.uuid4(), source_commit=commit, target_branch="continuity/x"
    ) as ws:
        assert ws.read_file("app/payments.py") == (source_repo / "app" / "payments.py").read_text()
        assert ws.exists("tests/test_payments.py")
        assert ws.record.source_commit == commit
        assert ws.record.target_branch == "continuity/x"


# --- write confinement ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../escaped.py",
        "../../escaped.py",
        "app/../../escaped.py",
        "/etc/passwd",
        "/usr/local/escaped.py",
    ],
)
async def test_a_write_outside_the_workspace_is_rejected(
    manager: WorkspaceManager, path: str
) -> None:
    """C7-01 acceptance.

    Absolute and traversing paths both. A migration that could write outside its
    workspace could modify the user's machine, which is the thing the workspace
    exists to make impossible.
    """
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        with pytest.raises(WorkspaceWriteRejected):
            ws.write_file(path, "malicious")

        assert path in ws.record.rejected_writes


async def test_a_symlinked_escape_is_rejected(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """The case a string-prefix check accepts.

    The path starts inside the workspace and resolves somewhere else entirely.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.py").write_text("original\n")

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        (ws.root / "link").symlink_to(outside, target_is_directory=True)

        assert str(ws.root / "link" / "target.py").startswith(str(ws.root))

        with pytest.raises(WorkspaceWriteRejected):
            ws.write_file("link/target.py", "overwritten")

    assert (outside / "target.py").read_text() == "original\n"


async def test_writes_inside_the_workspace_are_recorded(
    manager: WorkspaceManager,
) -> None:
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        ws.write_file("app/payments.py", "a\n")
        ws.write_file("app/nested/deep/new.py", "b\n")
        ws.write_file("app/payments.py", "c\n")

        assert ws.record.files_changed == ["app/payments.py", "app/nested/deep/new.py"]
        assert ws.read_file("app/nested/deep/new.py") == "b\n"


# --- the diff ------------------------------------------------------------


async def test_the_diff_comes_from_git_not_from_recorded_writes(
    manager: WorkspaceManager,
) -> None:
    """A change made by a command must appear in the patch.

    Assembling the diff from the writes we happened to record would omit
    anything a formatter or codemod did — and those are exactly the tools a
    migration reaches for.
    """
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        await ws.run(
            [
                "python",
                "-c",
                "import pathlib; p = pathlib.Path('app/payments.py');"
                " p.write_text('rewritten by a tool\\n')",
            ]
        )

        diff = await ws.diff()
        changed = await ws.changed_files()

    assert "rewritten by a tool" in diff
    assert changed == ["app/payments.py"]
    # Nothing went through `write_file`, so the recorded list is empty — which
    # is exactly why the diff may not be built from it.
    assert ws.record.files_changed == []


async def test_an_untouched_workspace_produces_an_empty_diff(
    manager: WorkspaceManager,
) -> None:
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        assert await ws.diff() == ""
        assert await ws.changed_files() == []


async def test_commands_run_in_the_workspace_are_recorded(
    manager: WorkspaceManager,
) -> None:
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        await ws.run(["python", "-c", "print('hello')"])

        recorded = [c for c in ws.record.commands_executed if "print('hello')" in str(c["argv"])]

    assert len(recorded) == 1
    assert recorded[0]["exit_code"] == 0
    assert recorded[0]["status"] == "completed"


async def test_a_workspace_command_carries_none_of_continuitys_secrets(
    manager: WorkspaceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor strips the environment; this proves the workspace uses it.

    A workspace that built its own `env` from `os.environ` would pass every
    other test here and hand a migration the deployment's credentials.
    """
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-real-but-shaped-like-one")
    monkeypatch.setenv("GEMINI_API_KEY", "also-not-real")

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        result = await ws.run(
            ["python", "-c", "import os; print('|'.join(sorted(os.environ)))"]
        )

    assert "AWS_SECRET_ACCESS_KEY" not in result.stdout
    assert "GEMINI_API_KEY" not in result.stdout


async def test_home_points_into_the_workspace(manager: WorkspaceManager) -> None:
    """A tool that writes a dotfile writes it here, not in the user's home."""
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        result = await ws.run(["python", "-c", "import os; print(os.environ['HOME'])"])

        assert result.stdout.strip() == str(ws.root)


# --- cleanup -------------------------------------------------------------


async def test_a_workspace_is_removed_on_success(manager: WorkspaceManager) -> None:
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        path = ws.root
        assert path.exists()

    assert not path.exists()


async def test_a_workspace_is_removed_on_failure(manager: WorkspaceManager) -> None:
    """Cleanup on success and on failure are the same line of code.

    An early return or a raised exception must not leave a checkout of the
    user's repository on disk.
    """
    path: Path | None = None

    with pytest.raises(RuntimeError, match="migration blew up"):
        async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
            path = ws.root
            ws.write_file("app/payments.py", "half-done\n")
            raise RuntimeError("migration blew up")

    assert path is not None
    assert not path.exists()


async def test_the_startup_sweep_reclaims_a_crashed_run(
    manager: WorkspaceManager,
) -> None:
    """C7-01 acceptance: the case a `finally` cannot cover.

    The process died, so nothing ran. The sweep is what makes a crash cost disk
    until the next start rather than forever. Simulated by creating a workspace
    and never closing it, which is what a crash leaves behind.
    """
    run_id = uuid.uuid4()
    context = manager.open(run_id=run_id, target_branch="continuity/x")
    workspace = await context.__aenter__()
    workspace.write_file("app/payments.py", "interrupted\n")
    path = workspace.root

    assert path.exists()

    swept = await manager.sweep_orphans()

    assert swept == [f"{WORKSPACE_PREFIX}{run_id}"]
    assert not path.exists()


async def test_the_sweep_leaves_directories_it_did_not_create(
    manager: WorkspaceManager,
) -> None:
    """Deleting something Continuity did not create would be unforgivable.

    The workspace root may be a shared temp directory. Only the prefix marks a
    directory as ours.
    """
    intruder = manager.workspace_root / "someone-elses-data"
    intruder.mkdir(parents=True)
    (intruder / "important.txt").write_text("do not delete")

    swept = await manager.sweep_orphans()

    assert swept == []
    assert (intruder / "important.txt").read_text() == "do not delete"


async def test_the_sweep_can_spare_recent_workspaces(
    manager: WorkspaceManager,
) -> None:
    """A sweep with an age floor must not delete a run in progress.

    Startup runs it with no floor, because nothing is in progress at startup.
    A periodic sweep would use one.
    """
    run_id = uuid.uuid4()
    context = manager.open(run_id=run_id, target_branch="continuity/x")
    workspace = await context.__aenter__()

    try:
        assert await manager.sweep_orphans(max_age_seconds=3600) == []
        assert workspace.root.exists()
    finally:
        await manager.cleanup(workspace.root)


async def test_cleanup_refuses_a_path_outside_the_workspace_root(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    """The most dangerous method in the file, guarded explicitly."""
    outside = tmp_path / "precious"
    outside.mkdir()

    with pytest.raises(WorkspaceError, match="outside the workspace root"):
        await manager.cleanup(outside)

    assert outside.exists()


async def test_reopening_a_run_id_reclaims_the_previous_workspace(
    manager: WorkspaceManager,
) -> None:
    """A retried run must not check out on top of a half-finished one."""
    run_id = uuid.uuid4()
    context = manager.open(run_id=run_id, target_branch="continuity/x")
    first = await context.__aenter__()
    first.write_file("app/payments.py", "stale\n")

    async with manager.open(run_id=run_id, target_branch="continuity/x") as second:
        assert "v1/charges" in second.read_file("app/payments.py")


# --- the allowlist -------------------------------------------------------


def test_git_worktree_is_allowlisted_narrowly() -> None:
    """Isolation needs `git worktree`; it does not need all of `git worktree`.

    Nothing in the permitted set reaches the network, and the paths they receive
    are constructed in `workspace.py`, never by a model.
    """
    git = ALLOWED_EXECUTABLES["git"]

    assert git is not None
    assert git["worktree"] == frozenset({"add", "remove", "prune", "list"})


def test_no_git_subcommand_reaches_the_network_or_rewrites_config() -> None:
    """The property the allowlist is actually protecting.

    `init` used to be in this list, on the reasoning that nothing needed it.
    C9-05 does: the evaluation harness lays a labelled fixture down as a real
    checkout, because the scanner and the workspace manager both work on real
    repositories. It was added deliberately, not to make a test pass — it
    creates a repository inside the confined cwd, reaches no network, and cannot
    touch Continuity's own repository.

    The rest stay out, and the reason is stated here so the next person needing
    one has to argue the same case rather than reading an unexplained list:
    `push`, `remote`, `clone`, `fetch`, and `pull` reach the network; `config`
    changes what every later command does; `submodule` does both.
    """
    git = ALLOWED_EXECUTABLES["git"]

    assert git is not None
    for forbidden in (
        "push",
        "remote",
        "config",
        "submodule",
        "clone",
        "fetch",
        "pull",
    ):
        assert forbidden not in git

    assert "init" in git, "C9-05 needs it; see the docstring above"


async def test_an_unlisted_worktree_action_is_refused(tmp_path: Path) -> None:
    """Two levels deep, so `git worktree` alone is not a loophole."""
    root = tmp_path / "ws"
    root.mkdir()
    executor = DevelopmentIsolatedExecutor(root, audit=NullExecutionAudit())

    for argv in (["git", "worktree"], ["git", "worktree", "repair"]):
        with pytest.raises(SubcommandNotAllowed):
            await executor.run(
                CommandSpec(
                    argv=argv,
                    cwd=root,
                    timeout_seconds=30,
                    env=workspace_env(root),
                    max_output_bytes=65_536,
                )
            )


# --- the hash ------------------------------------------------------------


def test_the_tree_hash_notices_content_and_layout(tmp_path: Path) -> None:
    """Guards the isolation assertions above against being vacuous.

    A hash that never changed would make every "the tree is untouched" test pass
    unconditionally.
    """
    root = tmp_path / "tree"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.py").write_text("one\n")
    baseline = tree_hash(root)

    (root / "a" / "x.py").write_text("two\n")
    assert tree_hash(root) != baseline

    (root / "a" / "x.py").write_text("one\n")
    assert tree_hash(root) == baseline

    # A move changes nothing about the bytes in aggregate, and everything about
    # the tree.
    (root / "a" / "x.py").rename(root / "x.py")
    assert tree_hash(root) != baseline


def test_the_tree_hash_ignores_git_internals(tmp_path: Path) -> None:
    """`.git` churns on its own; the user's *files* are the claim."""
    root = tmp_path / "tree"
    (root / ".git").mkdir(parents=True)
    (root / "code.py").write_text("x\n")
    baseline = tree_hash(root)

    (root / ".git" / "index").write_text("binary-ish")

    assert tree_hash(root) == baseline
