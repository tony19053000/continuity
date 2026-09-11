"""Turning a labelled case into something production code can actually run on.

Two pieces of scaffolding, and only two:

* a real git checkout of the case's `repo/`, because the scanner, the workspace
  manager, and the validator all work on a real repository and measuring a
  pretend one would measure the pretence;
* a provider adapter that serves the case's two specs, because the monitor's
  input is an adapter and nothing else.

Neither is a demo provider or a demo application — `CLAUDE.md` rule 13 keeps
those out of this repository. This adapter serves two JSON files from a fixture
directory and knows nothing about any company.

The git commands go through `ExecutionProvider` like every other command in
Continuity. The first version of this module reached for `subprocess.run`
directly and `tests/security/test_execution.py` refused it — correctly. The
point of that rule is precisely that a module written later must not quietly
open a second execution path, and "it is only test scaffolding" is exactly the
argument that would erode it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from backend.evaluation.labels import LabelledCase
from backend.models.enums import SourceKind
from backend.observability.execution_audit import NullExecutionAudit
from backend.providers.base import (
    BaseProviderAdapter,
    ExternalDocument,
    ProviderCapability,
    ProviderVersion,
)
from backend.shared.execution import CommandSpec, DevelopmentIsolatedExecutor

GIT_TIMEOUT_SECONDS = 60


class FixtureProviderAdapter(BaseProviderAdapter):
    """Serves one labelled case's specs, at whichever version it is asked for.

    `advance()` is what makes a case a *change*: the adapter reports
    `from_version` until it is advanced, and `to_version` afterwards, so the
    monitor sees a version move the way it would in the world.
    """

    capabilities = frozenset(
        {ProviderCapability.CURRENT_VERSION, ProviderCapability.OPENAPI_SPEC}
    )

    def __init__(self, case: LabelledCase) -> None:
        self.provider_id = case.provider_id
        self._case = case
        self._current = case.from_version

    def advance(self) -> None:
        self._current = self._case.to_version

    async def get_current_version(self) -> ProviderVersion:
        self._require(ProviderCapability.CURRENT_VERSION)
        return ProviderVersion(version=self._current, is_current=True)

    async def fetch_openapi_spec(self, version: ProviderVersion) -> ExternalDocument:
        self._require(ProviderCapability.OPENAPI_SPEC)
        return ExternalDocument(
            kind=SourceKind.OPENAPI_SPEC,
            content=json.dumps(self._case.spec(version.version), sort_keys=True),
            url=f"fixture://{self._case.case_id}/{version.version}.json",
            version=version.version,
        )


class FixtureCheckoutFailed(Exception):
    """A case's repository could not be laid down as a git checkout."""


def _fixture_env(root: Path) -> dict[str, str]:
    """The environment the fixture's git commands get.

    Identity is supplied here rather than with `git config`, which is
    deliberately not on the executable allowlist: a command that can write git
    config can change what later commands do. The global and system config
    files are pointed at nothing so a developer's own git settings cannot
    change an evaluation result.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "TMPDIR": str(root),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Continuity Evaluation",
        "GIT_AUTHOR_EMAIL": "evaluation@continuity.invalid",
        "GIT_COMMITTER_NAME": "Continuity Evaluation",
        "GIT_COMMITTER_EMAIL": "evaluation@continuity.invalid",
    }


async def materialise(case: LabelledCase, destination: Path) -> Path:
    """Copy the case's repository out and make it a real git checkout.

    A real one, with a commit, because the workspace manager creates worktrees
    from commits. A directory of files would fail at a stage the evaluation is
    not trying to measure.
    """
    root = destination / case.case_id
    shutil.copytree(case.repo, root)

    executor = DevelopmentIsolatedExecutor(destination, audit=NullExecutionAudit())
    env = _fixture_env(root)

    for argv in (
        ["git", "init", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", f"labelled case {case.case_id}"],
    ):
        result = await executor.run(
            CommandSpec(
                argv=argv,
                cwd=root,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                env=env,
                max_output_bytes=1_048_576,
            )
        )
        if result.exit_code != 0:
            raise FixtureCheckoutFailed(
                f"{' '.join(argv)} failed for {case.case_id}: "
                f"{result.stderr.strip()[:400]}"
            )

    return root
