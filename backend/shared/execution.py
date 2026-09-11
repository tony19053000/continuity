"""The only way any command runs (`03_SECURITY_ACCESS.md` §6).

There is no unrestricted shell execution anywhere in Continuity. Everything that
executes goes through `ExecutionProvider`, and the guarantees are structural
rather than conventional:

* **argv is a list.** There is no shell, so there is no quoting to get wrong and
  no metacharacter to escape. `asyncio.create_subprocess_exec` is the only spawn
  form in this module; `create_subprocess_shell`, `os.system`, and
  `subprocess.run(..., shell=True)` appear nowhere in the codebase, asserted by a
  test rather than by care.
* **The executable is allowlisted by bare name.** `argv[0]` may not contain a
  path separator, so a command cannot name a binary outside the resolved `PATH`.
* **`cwd` is resolved and confined.** Resolution follows symlinks, which is
  precisely how a symlinked escape out of the workspace is caught.
* **The environment is built, not inherited.** `os.environ` is never consulted.
  A variable Continuity's own process holds — an AWS key, a GitHub token, the
  database URL, the session secret — cannot reach a child, because nothing
  copies it there and names outside the allowlist are refused.
* **Timeouts and output caps are mandatory**, and a timeout kills the whole
  process group rather than the one process Continuity can see.
* **Every invocation is audited**, including every refusal. The audit sink has no
  default: an executor cannot be constructed without deciding where the record
  goes.

**Repository code is untrusted.** A test suite in an analyzed repository is
attacker-controlled code from Continuity's perspective, and that is exactly why
the environment is stripped and the credential surface is empty.

This is `DevelopmentIsolatedExecutor`: **process isolation, not sandboxing.** It
confines paths, strips credentials, and bounds resources. It does not defend
against a kernel exploit, and the UI labels it accordingly. Container-based and
enclave-backed providers are future implementations of the same protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.shared.errors import ContinuityError
from backend.shared.redaction import contains_secret, detected_secret_kinds, redact

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

#: Executables Continuity may run, by bare name. From `03_SECURITY_ACCESS.md` §6.
#: A value of `None` means any arguments; a set means the first argument must be
#: one of those subcommands.
ALLOWED_EXECUTABLES: Final[Mapping[str, frozenset[str] | None]] = {
    "pytest": None,
    "python": None,
    "python3": None,
    "npm": None,
    "npx": None,
    "node": None,
    "ruff": None,
    "mypy": None,
    "tsc": None,
    # `git` is the one executable that can reach outside the workspace or
    # rewrite history, so it is restricted to reads and local, additive writes.
    # Absent by design: `push` (delivery is branch-and-PR through the GitHub
    # App, never a shell), `remote` and `config` (both can redirect where code
    # goes or install a credential helper), `submodule` (fetches arbitrary
    # repositories), and anything that rewrites history.
    "git": frozenset(
        {
            "status",
            "diff",
            "add",
            "commit",
            "checkout",
            "switch",
            "branch",
            "rev-parse",
            "ls-files",
            "log",
            "show",
        }
    ),
}

#: Environment variables a child may receive. Allowlist, not denylist: a name
#: nobody thought to forbid is refused rather than forwarded, so the failure mode
#: of forgetting is a broken command instead of a leaked credential.
ALLOWED_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "CI",
        "NODE_ENV",
        "NO_COLOR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONHASHSEED",
        "PYTEST_ADDOPTS",
        "VIRTUAL_ENV",
        "npm_config_cache",
    }
)

#: Excerpt length in an audit row. The full output lives in the `CommandResult`
#: the caller holds; the audit records that something ran, and is not a log sink.
AUDIT_EXCERPT_LIMIT: Final = 2_000

#: Appended when output is cut, so a truncated log is never mistaken for a
#: complete one — a test suite that "passed" because its failures were cut off
#: is the failure mode this prevents.
TRUNCATION_MARKER: Final = "\n[continuity: output truncated, {omitted} bytes omitted]"

_READ_CHUNK: Final = 65_536


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExecutionRefused(ContinuityError):
    """Base for a command Continuity declined to run.

    Refusals are raised before the process is spawned, and audited.
    """

    code = "execution_refused"
    status_code = 403
    message = "The command was refused."


class ExecutableNotAllowed(ExecutionRefused):
    code = "executable_not_allowed"
    message = "That executable is not on the allowlist."


class SubcommandNotAllowed(ExecutionRefused):
    code = "subcommand_not_allowed"
    message = "That subcommand is not permitted for this executable."


class WorkspaceEscape(ExecutionRefused):
    code = "workspace_escape"
    message = "The working directory is outside the workspace root."


class EnvironmentNotAllowed(ExecutionRefused):
    code = "environment_not_allowed"
    message = "The command environment contains a variable that is not permitted."


class ExecutableNotFound(ExecutionRefused):
    code = "executable_not_found"
    status_code = 422
    message = "The executable could not be found on the command's PATH."


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"


class CommandSpec(BaseModel):
    """One command to run. Never a shell string."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: list[str] = Field(min_length=1)
    cwd: Path
    timeout_seconds: int = Field(ge=1, le=3600)
    #: The child's complete environment. Nothing is inherited, so this is the
    #: whole of it rather than an overlay.
    env: dict[str, str] = Field(default_factory=dict)
    max_output_bytes: int = Field(ge=1024)

    @field_validator("argv")
    @classmethod
    def _argv_is_usable(cls, argv: list[str]) -> list[str]:
        if any(not isinstance(item, str) for item in argv):
            raise ValueError("argv must contain only strings")
        if not argv[0].strip():
            raise ValueError("argv[0] must name an executable")
        # A separator in argv[0] would let a caller name a binary by path and
        # sidestep resolution through the allowlisted PATH.
        if "/" in argv[0] or "\\" in argv[0]:
            raise ValueError(
                "argv[0] must be a bare executable name, not a path: "
                f"{argv[0]!r}"
            )
        if "\x00" in "".join(argv):
            raise ValueError("argv must not contain null bytes")
        return argv

    @property
    def executable(self) -> str:
        return self.argv[0]

    def rendered(self) -> str:
        """A readable, secret-filtered rendering for logs and audit.

        Deliberately not shell-quoted and never re-parsed: this exists to be
        read by a person, and producing a string that looks runnable would
        invite someone to run it.
        """
        return redact(" ".join(self.argv))


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a command did."""

    argv: list[str]
    cwd: Path
    status: ExecutionStatus
    exit_code: int | None
    duration_ms: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ExecutionStatus.COMPLETED and self.exit_code == 0

    @property
    def timed_out(self) -> bool:
        return self.status is ExecutionStatus.TIMED_OUT


@dataclass(frozen=True, slots=True)
class ExecutionAuditRecord:
    """One audited execution attempt, whether it ran or was refused."""

    argv: list[str]
    cwd: str
    status: str
    exit_code: int | None = None
    duration_ms: int = 0
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    truncated: bool = False
    refused_reason: str | None = None


@runtime_checkable
class ExecutionAudit(Protocol):
    """Where execution records go.

    Required rather than optional: an executor that could be built without one
    would eventually be built without one.
    """

    async def record(self, entry: ExecutionAuditRecord) -> None: ...


@runtime_checkable
class ExecutionProvider(Protocol):
    async def run(self, spec: CommandSpec) -> CommandResult: ...


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


class DevelopmentIsolatedExecutor:
    """Process isolation, boundary enforcement, and a stripped environment.

    **Not a sandbox.** It confines paths, removes credentials, and bounds time
    and output. It does not contain a process that escapes the kernel's own
    boundaries, and Continuity never claims otherwise.
    """

    def __init__(self, workspace_root: Path, *, audit: ExecutionAudit) -> None:
        # Resolved once, here: every later confinement check compares against
        # this, so the root itself can never be a symlink pointing elsewhere.
        self._workspace_root = Path(workspace_root).resolve()
        if not self._workspace_root.is_dir():
            raise ValueError(f"workspace root is not a directory: {workspace_root}")
        self._audit = audit

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    async def run(self, spec: CommandSpec) -> CommandResult:
        try:
            cwd = self._resolve_cwd(spec.cwd)
            self._check_executable(spec)
            self._check_environment(spec)
            executable = self._resolve_executable(spec)
        except ExecutionRefused as refusal:
            await self._audit.record(
                ExecutionAuditRecord(
                    argv=list(spec.argv),
                    cwd=str(spec.cwd),
                    status="refused",
                    refused_reason=f"{refusal.code}: {refusal}",
                )
            )
            raise

        return await self._spawn(spec, executable=executable, cwd=cwd)

    # -- boundary checks --------------------------------------------------

    def _resolve_cwd(self, cwd: Path) -> Path:
        """Confine the working directory to the workspace.

        `resolve()` follows symlinks, which is the point: a directory inside the
        workspace that links to `/` resolves to `/` and is refused. Comparing
        the unresolved path would accept it.
        """
        resolved = Path(cwd).resolve()

        if not resolved.is_dir():
            raise WorkspaceEscape(f"The working directory does not exist: {cwd}")

        if resolved != self._workspace_root and self._workspace_root not in resolved.parents:
            raise WorkspaceEscape(
                f"{cwd} resolves to {resolved}, which is outside "
                f"{self._workspace_root}"
            )
        return resolved

    def _check_executable(self, spec: CommandSpec) -> None:
        if spec.executable not in ALLOWED_EXECUTABLES:
            raise ExecutableNotAllowed(
                f"{spec.executable!r} is not allowlisted. Allowed: "
                f"{sorted(ALLOWED_EXECUTABLES)}"
            )

        allowed_subcommands = ALLOWED_EXECUTABLES[spec.executable]
        if allowed_subcommands is None:
            return

        subcommand = next(
            (argument for argument in spec.argv[1:] if not argument.startswith("-")),
            None,
        )
        if subcommand is None or subcommand not in allowed_subcommands:
            raise SubcommandNotAllowed(
                f"{spec.executable} {subcommand!r} is not permitted. Allowed: "
                f"{sorted(allowed_subcommands)}"
            )

    def _check_environment(self, spec: CommandSpec) -> None:
        """Refuse any variable that is not allowlisted, and any secret value.

        The name check is the real control. The value check is defence in depth
        for the case that matters most: an allowlisted name — `PATH`, say —
        carrying something it should not.
        """
        forbidden = sorted(set(spec.env) - ALLOWED_ENV_NAMES)
        if forbidden:
            raise EnvironmentNotAllowed(
                f"environment variables not permitted in a workspace command: "
                f"{forbidden}"
            )

        for name, value in spec.env.items():
            if contains_secret(value):
                kinds = detected_secret_kinds(value)
                # The value itself is never echoed, here or anywhere.
                raise EnvironmentNotAllowed(
                    f"the value of {name} looks like a credential ({', '.join(kinds)})"
                )

        if "PATH" not in spec.env:
            raise EnvironmentNotAllowed(
                "PATH must be set explicitly; the child inherits nothing"
            )

    def _resolve_executable(self, spec: CommandSpec) -> str:
        """Find the binary on the command's own PATH, never Continuity's.

        `shutil.which` with an explicit `path=` is what keeps this honest — the
        default would consult `os.environ`, which is the thing this module
        exists to keep out of the child.
        """
        resolved = shutil.which(spec.executable, path=spec.env.get("PATH", ""))
        if resolved is None:
            raise ExecutableNotFound(
                f"{spec.executable!r} was not found on the command's PATH"
            )
        return resolved

    # -- spawning ---------------------------------------------------------

    async def _spawn(self, spec: CommandSpec, *, executable: str, cwd: Path) -> CommandResult:
        started = time.monotonic()

        process = await asyncio.create_subprocess_exec(
            executable,
            *spec.argv[1:],
            cwd=str(cwd),
            env=dict(spec.env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own process group, so a timeout kills the children a test
            # runner spawned too. Killing only the process Continuity can see
            # would leave the actual work running.
            start_new_session=True,
        )

        status = ExecutionStatus.COMPLETED
        try:
            stdout, stderr = await asyncio.wait_for(
                self._drain(process, spec.max_output_bytes),
                timeout=spec.timeout_seconds,
            )
            await process.wait()
        except TimeoutError:
            status = ExecutionStatus.TIMED_OUT
            stdout, stderr = await self._terminate(process, spec.max_output_bytes)
        except asyncio.CancelledError:
            # Cancellation must not orphan a process group.
            await self._terminate(process, spec.max_output_bytes)
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_text, stdout_truncated = self._render(stdout, spec.max_output_bytes)
        stderr_text, stderr_truncated = self._render(stderr, spec.max_output_bytes)

        result = CommandResult(
            argv=list(spec.argv),
            cwd=cwd,
            status=status,
            exit_code=None if status is ExecutionStatus.TIMED_OUT else process.returncode,
            duration_ms=duration_ms,
            stdout=stdout_text,
            stderr=stderr_text,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

        limit = AUDIT_EXCERPT_LIMIT
        await self._audit.record(
            ExecutionAuditRecord(
                argv=list(spec.argv),
                cwd=str(cwd),
                status=status.value,
                exit_code=result.exit_code,
                duration_ms=duration_ms,
                # Redacted: repository output is untrusted and may contain
                # anything, and the audit table is read into a browser.
                stdout_excerpt=redact(stdout_text[:limit]),
                stderr_excerpt=redact(stderr_text[:limit]),
                truncated=stdout_truncated or stderr_truncated,
            )
        )
        return result

    async def _drain(
        self, process: asyncio.subprocess.Process, cap: int
    ) -> tuple[tuple[bytes, int], tuple[bytes, int]]:
        # Both are always pipes: `_spawn` is the only caller and sets them so.
        # Handled rather than asserted, because ruff forbids `assert` in
        # backend code and a raised AssertionError would be a worse failure
        # here than an empty capture.
        empty: tuple[bytes, int] = (b"", 0)
        stdout = (
            _read_capped(process.stdout, cap)
            if process.stdout is not None
            else _completed(empty)
        )
        stderr = (
            _read_capped(process.stderr, cap)
            if process.stderr is not None
            else _completed(empty)
        )
        captured = await asyncio.gather(stdout, stderr)
        return captured[0], captured[1]

    async def _terminate(
        self, process: asyncio.subprocess.Process, cap: int
    ) -> tuple[tuple[bytes, int], tuple[bytes, int]]:
        """Kill the process group and collect whatever it managed to write."""
        _kill_group(process)

        empty: tuple[bytes, int] = (b"", 0)
        try:
            return await asyncio.wait_for(self._drain(process, cap), timeout=5)
        except (TimeoutError, asyncio.CancelledError, ProcessLookupError):
            return empty, empty
        finally:
            with contextlib.suppress(ProcessLookupError, asyncio.CancelledError):
                await asyncio.wait_for(process.wait(), timeout=5)

    @staticmethod
    def _render(captured: tuple[bytes, int], cap: int) -> tuple[str, bool]:
        data, total = captured
        text = data.decode("utf-8", errors="replace")
        if total <= cap:
            return text, False
        return text + TRUNCATION_MARKER.format(omitted=total - cap), True


async def _completed(value: tuple[bytes, int]) -> tuple[bytes, int]:
    """An already-finished capture, for the stream-is-None branch above."""
    return value


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, int]:
    """Keep the first `cap` bytes, count the rest, and never stop reading.

    Draining to the end matters as much as the cap does: a child that fills its
    pipe buffer blocks forever on the next write, so a reader that simply
    stopped at the cap would hang the command it was trying to bound.
    """
    kept = bytearray()
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            return bytes(kept), total
        total += len(chunk)
        if len(kept) < cap:
            kept.extend(chunk[: cap - len(kept)])


def _kill_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole group, falling back to the single process.

    SIGKILL rather than SIGTERM: this path is only reached after a deadline has
    already passed or the caller has been cancelled, and a handler that ignores
    SIGTERM would keep running.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        return
    with contextlib.suppress(ProcessLookupError):
        process.kill()
