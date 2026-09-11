"""A fixture repository and scripted engineer for C7-02 / C7-04.

The repository is real: a small integration with a test that fails against the
new contract. The engineer is scripted rather than live, because what these
tests assert is the *rules around* the patch — scope, secrets, tests,
dependencies, and the attempt budget — none of which are model behaviour.

The live path is proven separately against real Gemini.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.agents.contracts import (
    AnalyzedChange,
    FileEdit,
    ImpactAnalystOutput,
    MigrationEngineerOutput,
)
from backend.models.enums import ChangeType, Severity

#: The integration under migration. `charge()` omits `currency`, which the
#: provider's new contract requires.
CLIENT_V1 = '''\
REQUIRED_FIELDS = ["amount"]


def charge(amount):
    return {"amount": amount}
'''

#: What a correct migration produces.
CLIENT_V2 = '''\
REQUIRED_FIELDS = ["amount", "currency"]


def charge(amount, currency="usd"):
    return {"amount": amount, "currency": currency}
'''

#: A patch that changes the constant but not the function — passes nothing.
CLIENT_HALF_FIXED = '''\
REQUIRED_FIELDS = ["amount", "currency"]


def charge(amount):
    return {"amount": amount}
'''

SUITE = '''\
from app.client import REQUIRED_FIELDS, charge


def test_charge_sends_every_required_field():
    payload = charge(100)
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    assert not missing, f"missing {missing}"


def test_charge_returns_the_amount():
    assert charge(100)["amount"] == 100
'''


def write_fixture_repository(root: Path) -> None:
    """Lay down the project the migration operates on."""
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "client.py").write_text(CLIENT_V1)
    (root / "app" / "unrelated.py").write_text("VALUE = 1\n")
    (root / "tests" / "test_client.py").write_text(SUITE)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0"\ndependencies = ["httpx"]\n'
        "\n[tool.pytest.ini_options]\naddopts = \"-q\"\n"
    )


IMPACT_SET = ["app/client.py"]


def change() -> AnalyzedChange:
    """What the provider did, as the engineer needs to hear it."""
    return AnalyzedChange(
        change_type=ChangeType.REQUEST_FIELD_REQUIRED,
        resource="POST /v1/charges request.currency",
        breaking=True,
        security_relevant=False,
        authentication_relevant=False,
        rationale=(
            "The `currency` field on POST /v1/charges was optional in v1 and is "
            "required in v2. Requests without it are rejected."
        ),
    )


def impact() -> ImpactAnalystOutput:
    return ImpactAnalystOutput(
        relevant=True,
        severity=Severity.HIGH,
        migration_required=True,
        affected_files=["app/client.py"],
        affected_symbols=["app/client.py::charge"],
        affected_workflows=["Checkout"],
        affected_tests=["tests/test_client.py"],
        reasoning_summary="charge() omits the newly required currency field.",
    )


def edit(path: str, content: str, **extra: Any) -> FileEdit:
    return FileEdit(
        path=path,
        new_content=content,
        justification="adapts the client to the new contract",
        **extra,
    )


def output(*edits: FileEdit, **extra: Any) -> MigrationEngineerOutput:
    return MigrationEngineerOutput(
        plan_summary="Send currency on every charge.",
        edits=list(edits),
        **extra,
    )


def fix_it() -> MigrationEngineerOutput:
    return output(edit("app/client.py", CLIENT_V2))


def half_fix_it() -> MigrationEngineerOutput:
    return output(edit("app/client.py", CLIENT_HALF_FIXED))


def never_works() -> MigrationEngineerOutput:
    """A patch that keeps the suite red, however many times it is tried."""
    return output(edit("app/client.py", CLIENT_V1.replace("amount", "amount  ")))


class ScriptedEngineer:
    """Returns a canned output per attempt, recording what it was told."""

    def __init__(self, *outputs: Any) -> None:
        self._outputs = list(outputs)
        self.calls = 0
        self.previous_failures: list[str | None] = []
        self.prompts: list[str] = []

    async def run_structured(self, **kwargs: Any) -> Any:
        self.calls += 1
        prompt = str(kwargs.get("prompt", ""))
        self.prompts.append(prompt)
        self.previous_failures.append(
            prompt.split("The previous attempt failed. Evidence:")[1][:400]
            if "The previous attempt failed. Evidence:" in prompt
            else None
        )
        index = min(self.calls - 1, len(self._outputs) - 1)
        outcome = self._outputs[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StubProvider:
    @property
    def model_id(self) -> str:
        return "stub"

    def build_model(self, role: Any) -> Any:
        return object()


def test_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
