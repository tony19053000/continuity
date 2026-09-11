"""Stub every runtime agent at once, for tests about wiring rather than models.

The pipeline calls four agents in sequence. A test about whether the *stages
connect* should not depend on four live model calls — it would be slow, flaky,
and would prove nothing about the wiring when it failed.

Each stub returns the plausible answer for the fixture project: the change is
relevant, the failure is a missing field, the patch is clean. Tests that need a
different answer install their own.

The live path is proven separately, per phase, in `test_phase4_live.py` through
`test_phase7_live.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents.contracts import (
    ChangeScoutOutput,
    ImpactAnalystOutput,
    SecurityReviewerOutput,
    ValidatorOutput,
)
from backend.models.enums import PolicyDecision, Severity


class FixedRunner:
    """Returns one canned structured output, and counts the calls."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0

    async def run_structured(self, **_: Any) -> Any:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def impact_relevant() -> ImpactAnalystOutput:
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


def impact_irrelevant() -> ImpactAnalystOutput:
    return ImpactAnalystOutput(
        relevant=False,
        severity=Severity.INFO,
        migration_required=False,
        reasoning_summary="This project does not use the changed endpoint.",
    )


def clean_review() -> SecurityReviewerOutput:
    return SecurityReviewerOutput(
        findings=[],
        overall_recommendation=PolicyDecision.ALLOW,
        summary="Nothing of concern in this patch.",
    )


def validator_verdict() -> ValidatorOutput:
    return ValidatorOutput(
        build_ok=True,
        tests_passed=False,
        failure_summary="The request is missing the newly required field.",
        likely_causes=["currency is not being sent"],
        related_to_provider_change=True,
    )


def empty_scout() -> ChangeScoutOutput:
    """The differ found everything; the changelog adds nothing."""
    return ChangeScoutOutput(changes=[], injection_suspected=False)


def stub_agents(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, FixedRunner]:
    """Patch every runtime agent the pipeline touches.

    Returns the runners by name so a test can assert how often each was called
    — which is how "the stage ran" is distinguished from "the stage was skipped".
    """
    from backend.agents import change_scout as scout_module
    from backend.agents import impact_analyst as impact_module
    from backend.agents import security_reviewer as reviewer_module
    from backend.agents import validator as validator_module

    wiring = {
        "change_scout": (scout_module, "ChangeScoutAgent", empty_scout()),
        "impact_analyst": (impact_module, "ImpactAnalystAgent", impact_relevant()),
        "validator": (validator_module, "ValidatorAgent", validator_verdict()),
        "security_reviewer": (reviewer_module, "SecurityReviewerAgent", clean_review()),
    }

    runners: dict[str, FixedRunner] = {}
    for name, (module, attribute, default) in wiring.items():
        runner = FixedRunner(overrides.get(name, default))
        runners[name] = runner
        original = getattr(module, attribute)

        class Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, provider: Any, _runner: FixedRunner = runner, **kwargs: Any) -> None:
                super().__init__(provider, runner=_runner, **kwargs)

        monkeypatch.setattr(module, attribute, Patched)

    return runners
