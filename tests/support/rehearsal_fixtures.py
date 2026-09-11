"""A rehearsal adapter that produces a genuine difference, by really running.

The tests below execute pytest for real. A mocked executor would prove only
that the mock returned what the test told it to — and "the pass counts are
real" is precisely the property C6-04 is about.

The simulation mechanism is deliberately crude and entirely inside the
workspace: the adapter writes a `contract.json` describing one side of the
change, and the fixture test suite reads it and fails when the field it needs is
required but absent. That is enough to exercise the harness end to end without
Continuity holding any knowledge of a real provider, which stays out of scope
(`CLAUDE.md` §3.13).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from backend.models.schemas import ProviderChange
from backend.shared.execution import CommandSpec
from backend.validation.rehearsal import ContractSide

#: The fixture suite: two tests, one of which depends on the changed field.
SUITE = '''
import json
import pathlib

CONTRACT = json.loads((pathlib.Path(__file__).parent / "contract.json").read_text())


def test_charge_is_accepted():
    """Fails when `currency` became required and the client does not send it."""
    payload = {"amount": 100}
    missing = [f for f in CONTRACT["required"] if f not in payload]
    assert not missing, f"provider now requires {missing}"


def test_unrelated_behaviour():
    """Passes on both sides. Its job is to prove the delta is not everything."""
    assert CONTRACT["version"] in {"v1", "v2"}
'''


class ContractFileAdapter:
    """Writes the contract each side implies, then runs the suite."""

    can_simulate = True

    def __init__(self, *, difference: bool = True) -> None:
        #: When False, both sides get the same contract — the "ran but
        #: reproduced no difference" case, which must not become a migration.
        self._difference = difference
        self.prepared: list[ContractSide] = []

    async def prepare(
        self, side: ContractSide, workspace: Path, change: ProviderChange
    ) -> None:
        self.prepared.append(side)
        required = ["amount"]
        if self._difference and side is ContractSide.NEW:
            required.append("currency")

        (workspace / "contract.json").write_text(
            json.dumps({"version": "v1" if side is ContractSide.OLD else "v2",
                        "required": required})
        )

    def command(self, workspace: Path, selectors: list[str]) -> CommandSpec:
        return CommandSpec(
            argv=["python", "-m", "pytest", "-p", "no:cacheprovider", *selectors],
            cwd=workspace,
            timeout_seconds=120,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(workspace),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            max_output_bytes=1_048_576,
        )


class BrokenAdapter(ContractFileAdapter):
    """A suite that cannot run at all, on either side.

    Produces no counts, which must read as "we could not check" rather than
    "zero failures".
    """

    def command(self, workspace: Path, selectors: list[str]) -> CommandSpec:
        spec = super().command(workspace, selectors)
        return spec.model_copy(
            update={"argv": ["python", "-m", "pytest", "does_not_exist.py"]}
        )


def write_fixture_repository(workspace: Path) -> str:
    """Lay down the fixture suite. Returns the selector for it."""
    (workspace / "test_payments.py").write_text(SUITE)
    (workspace / "contract.json").write_text(
        json.dumps({"version": "v1", "required": ["amount"]})
    )
    return "test_payments.py"


def python_is_runnable() -> bool:
    """Whether `python` resolves on PATH, as the allowlist requires."""
    import shutil

    return shutil.which("python", path=os.environ.get("PATH", "")) is not None or bool(
        sys.executable
    )
