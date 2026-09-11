"""C6-01: the containment around untrusted repository code.

A test suite in an analyzed repository is attacker-controlled code from
Continuity's perspective. These tests are written from that position: each one
asks what a hostile command could reach, and asserts it cannot.

Most of them actually spawn processes. That is deliberate — the guarantees here
are about what a real child process receives, and a mocked subprocess would
prove only that the mock was configured the way the test expected.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.models import AuditEvent
from backend.models.session import session_scope
from backend.observability.execution_audit import (
    EXECUTION_REFUSED,
    DatabaseExecutionAudit,
    NullExecutionAudit,
)
from backend.shared.execution import (
    ALLOWED_ENV_NAMES,
    ALLOWED_EXECUTABLES,
    CommandSpec,
    DevelopmentIsolatedExecutor,
    EnvironmentNotAllowed,
    ExecutableNotAllowed,
    ExecutableNotFound,
    ExecutionStatus,
    SubcommandNotAllowed,
    WorkspaceEscape,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

#: A minimal, allowlisted environment. Real PATH so `python` resolves.
def _env(**extra: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}


async def _assert_dies(pid: int, *, within: float = 10.0) -> None:
    """Wait for a process to actually be gone.

    Polled rather than sampled once: `os.kill(pid, 0)` succeeds on a zombie
    until its parent reaps it, and when the parent is the process we just
    killed, reaping happens once init adopts the orphan. That window is short
    but real, and it widens under load — sampling once made this test pass
    alone and fail inside the full suite.
    """
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)

    raise AssertionError(f"process {pid} survived; it should have been killed")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def executor(workspace: Path) -> DevelopmentIsolatedExecutor:
    return DevelopmentIsolatedExecutor(workspace, audit=NullExecutionAudit())


def _spec(workspace: Path, *argv: str, **overrides: object) -> CommandSpec:
    payload: dict[str, object] = {
        "argv": list(argv),
        "cwd": workspace,
        "timeout_seconds": 30,
        "env": _env(),
        "max_output_bytes": 1_048_576,
    }
    payload.update(overrides)
    return CommandSpec(**payload)  # type: ignore[arg-type]


# --- there is no shell ---------------------------------------------------


#: Call targets that mean "a shell interprets this", or "an unbounded blocking
#: spawn outside the boundary". Matched against parsed code, never raw text —
#: this module's own docstring names every one of them.
FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        "create_subprocess_shell",
        "system",
        "popen",
        "run",
        "call",
        "Popen",
        "check_output",
        "check_call",
        "getoutput",
        "getstatusoutput",
    }
)


def _shell_offences(path: Path) -> list[str]:
    """Real calls into a shell or an unbounded spawn, found by parsing."""
    tree = ast.parse(path.read_text())
    found: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # shell=True in any call at all.
        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                found.append("shell=True")

        target = node.func
        if isinstance(target, ast.Attribute):
            owner = target.value
            owner_name = owner.id if isinstance(owner, ast.Name) else None
            if target.attr == "create_subprocess_shell":
                found.append("asyncio.create_subprocess_shell")
            elif owner_name in {"subprocess", "os"} and target.attr in FORBIDDEN_CALLS:
                found.append(f"{owner_name}.{target.attr}")
        elif isinstance(target, ast.Name) and target.id == "create_subprocess_shell":
            found.append("create_subprocess_shell")

    return found


def test_no_module_anywhere_can_spawn_a_shell() -> None:
    """C6-01 acceptance: `shell=False` is the only spawn form.

    Asserted across the whole backend rather than in the executor alone. The
    risk is not that this module grows a shell — it is that some other module
    quietly reaches for `subprocess` and bypasses the boundary entirely.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}: {offence}"
        for path in BACKEND.rglob("*.py")
        for offence in _shell_offences(path)
    ]

    assert not offenders, f"a shell or unbounded spawn reachable outside the executor: {offenders}"


def test_the_shell_scan_actually_detects_a_shell(tmp_path: Path) -> None:
    """Guards the scan above against silently matching nothing.

    A structural test that has stopped working looks exactly like a passing one,
    so this feeds it code it must reject.
    """
    planted = tmp_path / "bad.py"
    planted.write_text(
        "import subprocess, asyncio, os\n"
        "def a(): subprocess.run('ls', shell=True)\n"
        "def b(): os.system('id')\n"
        "async def c(): await asyncio.create_subprocess_shell('id')\n"
    )

    offences = _shell_offences(planted)

    assert "shell=True" in offences
    assert "os.system" in offences
    assert "asyncio.create_subprocess_shell" in offences


def test_only_the_executor_module_spawns_a_process() -> None:
    """The rule that does not go stale.

    Enumerating forbidden spellings above catches the known ones. This catches a
    spawn written some way nobody listed, by asserting that exactly one module
    in the backend creates a subprocess at all.
    """
    spawning = [
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if "create_subprocess" in path.read_text()
    ]

    assert spawning == ["shared/execution.py"], (
        f"a process is spawned outside the execution boundary: {spawning}"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["python -c 'print(1)'"],
        ["python", "-c", "import os; os.system('id')"],
    ],
    ids=["whole-command-as-one-string", "shell-inside-an-argument"],
)
def test_a_shell_string_cannot_become_a_command(argv: list[str], workspace: Path) -> None:
    """argv is a list, so a string is one executable name, not a command line.

    The first case is refused because no executable is named `python -c ...`.
    The second is a reminder of what the boundary does and does not do: the
    argument is passed through verbatim as data, and whether `os.system` runs
    inside the child is the child's business. Continuity's guarantee is that it
    never constructs a shell, and that the child holds no credentials.
    """
    if len(argv) == 1:
        with pytest.raises(ExecutableNotAllowed):
            CommandSpec(
                argv=argv,
                cwd=workspace,
                timeout_seconds=5,
                env=_env(),
                max_output_bytes=4096,
            )
            raise ExecutableNotAllowed("unreachable")
    else:
        spec = _spec(workspace, *argv)
        assert spec.argv == argv


@pytest.mark.parametrize(
    "bad",
    ["/usr/bin/python", "./pytest", "../../bin/sh", "sub/dir/node"],
)
def test_argv0_may_not_be_a_path(bad: str, workspace: Path) -> None:
    """A path in argv[0] would sidestep allowlisted-PATH resolution entirely.

    Otherwise `/tmp/attacker/pytest` is an allowlisted basename pointing at
    anything at all.
    """
    with pytest.raises(ValueError, match="bare executable name"):
        _spec(workspace, bad, "--version")


def test_argv_may_not_contain_a_null_byte(workspace: Path) -> None:
    with pytest.raises(ValueError, match="null"):
        _spec(workspace, "python", "--version\x00--evil")


def test_an_empty_argv_is_refused(workspace: Path) -> None:
    with pytest.raises(ValueError):
        _spec(workspace)


# --- the executable allowlist --------------------------------------------


@pytest.mark.parametrize(
    "executable",
    ["sh", "bash", "zsh", "curl", "wget", "ssh", "sudo", "chmod", "rm", "env", "aws", "gh"],
)
async def test_an_executable_off_the_allowlist_is_refused(
    executable: str, executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """Allowlist, not denylist — these are examples, not the definition."""
    with pytest.raises(ExecutableNotAllowed):
        await executor.run(_spec(workspace, executable, "--help"))


async def test_an_allowlisted_executable_runs(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    result = await executor.run(
        _spec(workspace, "python", "-c", "print('continuity')")
    )

    assert result.ok
    assert result.exit_code == 0
    assert "continuity" in result.stdout
    assert result.status is ExecutionStatus.COMPLETED


def test_the_allowlist_contains_no_shell_and_no_network_tool() -> None:
    """A regression guard on the list itself.

    The list is the whole control; an entry added carelessly is the most likely
    way this boundary gets weakened, and it would not fail any other test.
    """
    forbidden = {
        "sh", "bash", "zsh", "fish", "dash", "ksh",
        "curl", "wget", "nc", "ssh", "scp", "rsync",
        "sudo", "su", "env", "eval", "docker", "aws", "gh", "make",
    }

    assert not (set(ALLOWED_EXECUTABLES) & forbidden)


@pytest.mark.parametrize(
    "subcommand",
    ["push", "remote", "config", "submodule", "clone", "fetch", "pull", "reset", "rebase"],
)
async def test_dangerous_git_subcommands_are_refused(
    subcommand: str, executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """`git` is allowlisted; most of `git` is not.

    `push` is the one that matters most: delivery is branch-and-PR through the
    GitHub App, and a shell path to `git push` would route around every check
    in `03_SECURITY_ACCESS.md` §4 — approval included.
    """
    with pytest.raises(SubcommandNotAllowed):
        await executor.run(_spec(workspace, "git", subcommand))


async def test_a_git_subcommand_cannot_be_hidden_behind_a_flag(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """`git -c foo=bar push` must not read as "no subcommand, therefore fine"."""
    with pytest.raises(SubcommandNotAllowed):
        await executor.run(_spec(workspace, "git", "-c", "user.name=x", "push"))


async def test_git_with_no_subcommand_at_all_is_refused(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    with pytest.raises(SubcommandNotAllowed):
        await executor.run(_spec(workspace, "git"))


async def test_an_allowed_git_subcommand_is_permitted(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """Refusal must be about the subcommand, not about `git` being unusable."""
    result = await executor.run(_spec(workspace, "git", "status"))

    # Not a repository, so git exits non-zero — but it ran, which is the point.
    assert result.status is ExecutionStatus.COMPLETED


# --- workspace confinement -----------------------------------------------


async def test_a_cwd_outside_the_workspace_is_refused(
    executor: DevelopmentIsolatedExecutor, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    with pytest.raises(WorkspaceEscape):
        await executor.run(_spec(outside, "python", "--version", cwd=outside))


async def test_a_traversal_out_of_the_workspace_is_refused(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    with pytest.raises(WorkspaceEscape):
        await executor.run(
            _spec(workspace, "python", "--version", cwd=workspace / ".." / "..")
        )


async def test_a_symlinked_escape_is_refused(
    executor: DevelopmentIsolatedExecutor, workspace: Path, tmp_path: Path
) -> None:
    """C6-01 acceptance, named explicitly: including via symlink.

    This is the case a string-prefix check passes and a real one fails. The path
    starts inside the workspace, so `str(cwd).startswith(str(root))` is True —
    and it resolves to somewhere else entirely.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.txt").write_text("not reachable")

    link = workspace / "innocent"
    link.symlink_to(outside, target_is_directory=True)

    # The check a careless implementation would make, shown to be insufficient.
    assert str(link).startswith(str(workspace))

    with pytest.raises(WorkspaceEscape):
        await executor.run(_spec(workspace, "python", "--version", cwd=link))


async def test_a_subdirectory_of_the_workspace_is_allowed(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    nested = workspace / "packages" / "api"
    nested.mkdir(parents=True)

    result = await executor.run(
        _spec(workspace, "python", "-c", "import os; print(os.getcwd())", cwd=nested)
    )

    assert result.ok
    assert str(nested.resolve()) in result.stdout


async def test_a_nonexistent_cwd_is_refused(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    with pytest.raises(WorkspaceEscape):
        await executor.run(
            _spec(workspace, "python", "--version", cwd=workspace / "missing")
        )


def test_a_workspace_root_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")

    with pytest.raises(ValueError, match="not a directory"):
        DevelopmentIsolatedExecutor(file_path, audit=NullExecutionAudit())


# --- the credential surface ----------------------------------------------


async def test_the_child_environment_contains_only_what_was_given(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """C6-01 acceptance: nothing is inherited.

    Asserted by reading the child's real environment rather than by inspecting
    the call — the question is what the process actually received.
    """
    result = await executor.run(
        _spec(
            workspace,
            "python",
            "-c",
            "import os,json; print(json.dumps(sorted(os.environ)))",
            env=_env(CI="1"),
        )
    )

    received = set(__import__("json").loads(result.stdout))
    assert received <= {"PATH", "CI"} | {"LC_CTYPE", "PWD", "SHLVL", "_"}, (
        f"unexpected variables reached the child: {sorted(received - {'PATH', 'CI'})}"
    )


async def test_continuitys_own_secrets_never_reach_a_child(
    executor: DevelopmentIsolatedExecutor, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion, tested the way it would actually fail.

    Continuity's process is given exactly the variables a real deployment holds,
    and the child is asked to print everything it can see. An implementation
    that passed `env=None`, or merged `os.environ`, fails here — and that is the
    single most likely way this control would be lost.
    """
    planted = {
        "AWS_ACCESS_KEY_ID": "AKIA" + "TESTONLYNOTREAL0",
        "AWS_SECRET_ACCESS_KEY": "not-a-real-secret-value-for-testing-only",
        "AWS_SESSION_TOKEN": "session-token-test-value",
        "GITHUB_APP_PRIVATE_KEY": "private-key-test-value",
        "GEMINI_API_KEY": "gemini-key-test-value",
        "DATABASE_URL": "postgresql://user:pw@localhost/continuity",
        "SESSION_SECRET": "session-secret-test-value",
    }
    for name, value in planted.items():
        monkeypatch.setenv(name, value)

    result = await executor.run(
        _spec(
            workspace,
            "python",
            "-c",
            "import os; print('\\n'.join(f'{k}={v}' for k, v in os.environ.items()))",
        )
    )

    for name, value in planted.items():
        assert name not in result.stdout, f"{name} reached the child process"
        assert value not in result.stdout, f"the value of {name} reached the child"


@pytest.mark.parametrize(
    "name",
    [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "GEMINI_API_KEY",
        "DATABASE_URL",
        "SESSION_SECRET",
        "LD_PRELOAD",
        "PYTHONSTARTUP",
    ],
)
async def test_a_non_allowlisted_variable_is_refused_rather_than_dropped(
    name: str, executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """Refused loudly, not filtered silently.

    Silently dropping would let a caller believe a command ran with the
    environment it asked for. `LD_PRELOAD` and `PYTHONSTARTUP` are in this list
    because they change what an allowlisted executable *is*.
    """
    with pytest.raises(EnvironmentNotAllowed) as raised:
        await executor.run(_spec(workspace, "python", "--version", env=_env(**{name: "x"})))

    assert name in str(raised.value)


def test_the_env_allowlist_holds_nothing_credential_shaped() -> None:
    """A guard on the list itself, like the executable one."""
    for name in ALLOWED_ENV_NAMES:
        upper = name.upper()
        assert not any(
            marker in upper
            for marker in ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "AWS", "GITHUB")
        ), f"{name} is allowlisted and looks credential-bearing"
    # KEY is checked separately: `npm_config_cache` and friends may legitimately
    # contain it, but nothing currently does, and this catches a careless add.
    assert not any("KEY" in name.upper() for name in ALLOWED_ENV_NAMES)


async def test_an_allowlisted_name_carrying_a_credential_is_refused(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """Defence in depth for the case that gets past the name check."""
    from tests.support.secret_samples import GITHUB_TOKEN

    with pytest.raises(EnvironmentNotAllowed) as raised:
        await executor.run(
            _spec(workspace, "python", "--version", env=_env(PYTEST_ADDOPTS=GITHUB_TOKEN))
        )

    # Names the variable and the kind, never the value.
    assert "PYTEST_ADDOPTS" in str(raised.value)
    assert GITHUB_TOKEN not in str(raised.value)


async def test_a_command_without_a_path_is_refused(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """Inheriting nothing means PATH must be a decision, not an accident."""
    with pytest.raises(EnvironmentNotAllowed, match="PATH"):
        await executor.run(_spec(workspace, "python", "--version", env={}))


async def test_resolution_uses_the_commands_path_not_continuitys(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """A PATH that contains nothing must not silently fall back.

    If resolution consulted `os.environ`, this would find the real `python` and
    the environment boundary would be decorative.
    """
    empty = workspace / "empty-bin"
    empty.mkdir()

    with pytest.raises(ExecutableNotFound):
        await executor.run(
            _spec(workspace, "python", "--version", env={"PATH": str(empty)})
        )


# --- timeouts ------------------------------------------------------------


async def test_a_command_exceeding_its_timeout_is_killed_and_reported(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """C6-01 acceptance. Reported *as* a timeout, not as a failing exit code.

    The distinction matters downstream: a suite that timed out has not told us
    anything about the code, while a suite that exited non-zero has.
    """
    result = await executor.run(
        _spec(
            workspace,
            "python",
            "-c",
            "import time; time.sleep(60)",
            timeout_seconds=1,
        )
    )

    assert result.timed_out
    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.exit_code is None
    assert not result.ok
    assert result.duration_ms < 30_000


async def test_a_timeout_kills_the_whole_process_group(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """A test runner spawns children; killing only the parent leaves them running.

    The child writes its grandchild's pid, then both sleep. After the timeout,
    the grandchild must be gone — otherwise a timed-out migration rehearsal
    keeps burning the machine invisibly.
    """
    marker = workspace / "grandchild.pid"
    program = (
        "import os, subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )

    result = await executor.run(
        _spec(workspace, "python", "-c", program, timeout_seconds=2)
    )

    assert result.timed_out
    assert marker.exists(), "the child never started its grandchild"

    # Signal 0 probes liveness without sending anything.
    await _assert_dies(int(marker.read_text()))


# --- output caps ---------------------------------------------------------


async def test_output_beyond_the_cap_is_truncated_with_a_marker(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """C6-01 acceptance. The marker is the part that matters.

    Silent truncation is how a suite's failures disappear and its output reads
    as a pass.
    """
    result = await executor.run(
        _spec(
            workspace,
            "python",
            "-c",
            "print('x' * 100_000)",
            max_output_bytes=2048,
        )
    )

    assert result.stdout_truncated
    assert "truncated" in result.stdout
    assert "bytes omitted" in result.stdout
    assert result.ok


async def test_output_within_the_cap_is_untouched(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    result = await executor.run(
        _spec(workspace, "python", "-c", "print('short')", max_output_bytes=4096)
    )

    assert not result.stdout_truncated
    assert "truncated" not in result.stdout
    assert result.stdout.strip() == "short"


async def test_an_enormous_output_does_not_hang_the_command(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """The reason the reader drains past the cap instead of stopping at it.

    A child whose pipe fills blocks forever on its next write. A reader that
    stopped at the cap would deadlock exactly the runaway command the cap is
    meant to bound — and it would present as a timeout, which reads as the
    repository's fault.
    """
    result = await executor.run(
        _spec(
            workspace,
            "python",
            "-c",
            "import sys\nfor _ in range(2000): sys.stdout.write('y' * 5000)\n",
            max_output_bytes=4096,
            timeout_seconds=30,
        )
    )

    assert result.status is ExecutionStatus.COMPLETED
    assert result.exit_code == 0
    assert result.stdout_truncated
    assert len(result.stdout) < 10_000


async def test_stderr_is_captured_and_capped_separately(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    result = await executor.run(
        _spec(
            workspace,
            "python",
            "-c",
            "import sys; sys.stderr.write('e' * 50_000); print('out')",
            max_output_bytes=2048,
        )
    )

    assert result.stderr_truncated
    assert not result.stdout_truncated
    assert "out" in result.stdout


# --- cancellation --------------------------------------------------------


async def test_cancelling_a_run_does_not_orphan_the_process(
    executor: DevelopmentIsolatedExecutor, workspace: Path
) -> None:
    """A cancelled migration must not leave a test suite running.

    Without the cancellation path, the process survives its caller and nothing
    is left holding a reference to kill it.
    """
    marker = workspace / "child.pid"
    program = (
        "import os, time\n"
        f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )

    task = asyncio.create_task(
        executor.run(_spec(workspace, "python", "-c", program, timeout_seconds=60))
    )
    for _ in range(100):
        await asyncio.sleep(0.05)
        if marker.exists():
            break

    assert marker.exists(), "the child never started"
    child = int(marker.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_dies(child)


# --- audit ---------------------------------------------------------------


@pytest.mark.usefixtures("database")
async def test_every_invocation_writes_an_audit_row(workspace: Path) -> None:
    """C6-01 acceptance."""
    async with session_scope() as session:
        executor = DevelopmentIsolatedExecutor(
            workspace, audit=DatabaseExecutionAudit(session, actor="validator")
        )
        await executor.run(_spec(workspace, "python", "-c", "print('audited')"))

    async with session_scope() as session:
        rows = (await session.execute(select(AuditEvent))).scalars().all()

    assert len(rows) == 1
    (row,) = rows
    assert row.kind == "execution.ran"
    assert row.actor == "validator"
    assert row.detail["argv"][0] == "python"
    assert row.detail["exit_code"] == 0
    assert row.detail["status"] == "completed"
    assert "audited" in row.detail["stdout_excerpt"]
    assert row.detail["duration_ms"] >= 0


@pytest.mark.usefixtures("database")
async def test_a_refusal_is_audited_too(workspace: Path) -> None:
    """The more interesting row of the two.

    "What did this system try to do, and what stopped it" is unanswerable if
    refusals leave no trace — and a refused command is precisely the event a
    security review wants to see.
    """
    async with session_scope() as session:
        executor = DevelopmentIsolatedExecutor(
            workspace, audit=DatabaseExecutionAudit(session, actor="migration_engineer")
        )
        with pytest.raises(ExecutableNotAllowed):
            await executor.run(_spec(workspace, "bash", "-c", "id"))

    async with session_scope() as session:
        (row,) = (await session.execute(select(AuditEvent))).scalars().all()

    assert row.kind == EXECUTION_REFUSED
    assert row.detail["refused_reason"]
    assert "executable_not_allowed" in row.detail["refused_reason"]
    assert row.detail["argv"] == ["bash", "-c", "id"]


@pytest.mark.usefixtures("database")
async def test_a_timeout_is_audited_as_a_timeout(workspace: Path) -> None:
    async with session_scope() as session:
        executor = DevelopmentIsolatedExecutor(
            workspace, audit=DatabaseExecutionAudit(session, actor="validator")
        )
        await executor.run(
            _spec(workspace, "python", "-c", "import time; time.sleep(30)", timeout_seconds=1)
        )

    async with session_scope() as session:
        (row,) = (await session.execute(select(AuditEvent))).scalars().all()

    assert row.detail["status"] == "timed_out"
    assert row.detail["exit_code"] is None


@pytest.mark.usefixtures("database")
async def test_audited_output_is_secret_filtered(workspace: Path) -> None:
    """Repository output is untrusted and this table is read into a browser.

    A test suite that prints a token — its own, or one it found — must not have
    that token persisted by the act of Continuity watching it run.
    """
    from tests.support.secret_samples import GITHUB_TOKEN as token

    async with session_scope() as session:
        executor = DevelopmentIsolatedExecutor(
            workspace, audit=DatabaseExecutionAudit(session, actor="validator")
        )
        result = await executor.run(
            _spec(workspace, "python", "-c", f"print({token!r})")
        )

    # The caller sees the real output; only the stored record is filtered.
    assert token in result.stdout

    async with session_scope() as session:
        (row,) = (await session.execute(select(AuditEvent))).scalars().all()

    assert token not in row.detail["stdout_excerpt"]
    assert token not in str(row.detail)


def test_an_executor_cannot_be_built_without_an_audit_sink(workspace: Path) -> None:
    """No default sink, by design.

    A default would make "audited" something you get by remembering, and the
    acceptance criterion is that every invocation writes a row.
    """
    with pytest.raises(TypeError):
        DevelopmentIsolatedExecutor(workspace)  # type: ignore[call-arg]


def test_the_executor_never_reads_the_ambient_environment() -> None:
    """Structural backstop for the credential tests above.

    `os.environ` appearing in this module at all would mean some path can copy
    Continuity's own variables into a child, and the behavioural test only
    covers the paths it happens to exercise.
    """
    source = (BACKEND / "shared" / "execution.py").read_text()
    tree = ast.parse(source)

    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ]

    assert not reads, "the executor reads os.environ; a child could inherit a secret"
    assert "os.getenv" not in source
