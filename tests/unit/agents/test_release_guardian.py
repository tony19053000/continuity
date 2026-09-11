"""C9-02: the comparison, and what it refuses to conclude.

The Guardian's value is in what it declines to blame on the migration. A check
that was red before the merge is the project's existing problem; calling it a
regression would teach a team to ignore these reports, which is worse than not
producing them.
"""

from __future__ import annotations

import pytest

from backend.agents.contracts import ReleaseGuardianOutput
from backend.agents.release_guardian import assess_release
from backend.verification.environment import CheckOutcome, Observation
from tests.support.agent_stubs import FixedRunner


def _observation(*checks: tuple[str, bool], reached: bool = True) -> Observation:
    return Observation(
        reached=reached,
        outcomes=[
            CheckOutcome(
                name=name,
                ok=ok,
                status=200 if ok else 500,
                reason="as expected" if ok else "expected HTTP 200, got 500",
            )
            for name, ok in checks
        ],
    )


async def _assess(before: Observation | None, after: Observation, **kwargs: object):
    return await assess_release(
        provider_id="acmepay",
        from_version="v1",
        to_version="v2",
        before=before,
        after=after,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_a_healthy_release_passes() -> None:
    assessment = await _assess(
        _observation(("health", True), ("checkout", True)),
        _observation(("health", True), ("checkout", True)),
    )

    assert assessment.passed
    assert assessment.regressions == []
    assert assessment.compared_with_baseline
    assert "2 declared check(s) passed" in assessment.summary


async def test_a_check_that_broke_is_a_regression() -> None:
    assessment = await _assess(
        _observation(("health", True), ("checkout", True)),
        _observation(("health", True), ("checkout", False)),
    )

    assert not assessment.passed
    assert [item.name for item in assessment.regressions] == ["checkout"]
    assert assessment.regressions[0].status == 500


async def test_a_check_that_was_already_failing_is_not_blamed_on_the_migration() -> None:
    """The reason the before-observation exists at all."""
    assessment = await _assess(
        _observation(("health", True), ("checkout", False)),
        _observation(("health", True), ("checkout", False)),
    )

    assert assessment.regressions == []
    assert assessment.already_failing == ["checkout"]
    assert assessment.passed, "nothing this migration did broke"


async def test_a_failure_with_no_baseline_fails_closed_and_says_why() -> None:
    """"We cannot tell" is a reason to show a person, not to call it fine."""
    assessment = await _assess(None, _observation(("checkout", False)))

    assert not assessment.passed
    assert assessment.unattributable == ["checkout"]
    assert assessment.regressions == []
    assert not assessment.compared_with_baseline


async def test_an_unreachable_environment_verifies_nothing() -> None:
    assessment = await _assess(
        _observation(("health", True)),
        Observation(reached=False, note="the environment could not be reached"),
    )

    assert not assessment.passed
    assert "could not be reached" in assessment.summary
    assert assessment.regressions == []


async def test_the_report_says_no_action_was_taken() -> None:
    assessment = await _assess(
        _observation(("health", True)), _observation(("health", True))
    )

    assert assessment.report()["action_taken"].startswith("none")
    assert assessment.report()["rollback_recommended"] is False


# --- The agent half -------------------------------------------------------


class _Provider:
    def build_model(self, role: object) -> object:
        return object()


def _agent_returning(outcome: object) -> type:
    from backend.agents.specialists import ReleaseGuardianAgent

    class Patched(ReleaseGuardianAgent):  # type: ignore[misc]
        def __init__(self, provider: object, **kwargs: object) -> None:
            kwargs.pop("runner", None)
            super().__init__(provider, runner=FixedRunner(outcome), **kwargs)  # type: ignore[arg-type]

    return Patched


async def test_the_agent_cannot_turn_a_regression_into_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict is arithmetic over two observations, not an opinion."""
    from backend.agents import release_guardian as module

    monkeypatch.setattr(
        module,
        "ReleaseGuardianAgent",
        _agent_returning(
            ReleaseGuardianOutput(
                regression_summary="Nothing is wrong, this is fine.",
                likely_related_to_migration=False,
                rollback_recommended=False,
                rationale="I see no problem.",
            )
        ),
    )

    assessment = await _assess(
        _observation(("checkout", True)),
        _observation(("checkout", False)),
        model_provider=_Provider(),
    )

    assert not assessment.passed
    assert assessment.model_consulted
    assert [item.name for item in assessment.regressions] == ["checkout"]


async def test_a_rollback_recommendation_is_recorded_and_nothing_happens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import release_guardian as module

    monkeypatch.setattr(
        module,
        "ReleaseGuardianAgent",
        _agent_returning(
            ReleaseGuardianOutput(
                regression_summary="Checkout returns 500 on every request.",
                likely_related_to_migration=True,
                rollback_recommended=True,
                rationale="The migration changed the charge payload.",
            )
        ),
    )

    assessment = await _assess(
        _observation(("checkout", True)),
        _observation(("checkout", False)),
        model_provider=_Provider(),
    )

    assert assessment.rollback_recommended is True
    assert assessment.report()["action_taken"] == (
        "none: Continuity never performs a rollback"
    )


async def test_a_model_failure_leaves_the_comparison_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.agents import release_guardian as module

    monkeypatch.setattr(
        module, "ReleaseGuardianAgent", _agent_returning(RuntimeError("unavailable"))
    )

    assessment = await _assess(
        _observation(("checkout", True)),
        _observation(("checkout", False)),
        model_provider=_Provider(),
    )

    assert not assessment.model_consulted
    assert [item.name for item in assessment.regressions] == ["checkout"]
    assert "check comparison alone" in assessment.rationale


async def test_the_agent_is_not_consulted_about_an_environment_nobody_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is nothing to interpret, and a model asked anyway would invent it."""
    from backend.agents import release_guardian as module

    runner_used = False

    def _fail(*_: object, **__: object) -> object:
        nonlocal runner_used
        runner_used = True
        raise AssertionError("the agent should not have been constructed")

    monkeypatch.setattr(module, "ReleaseGuardianAgent", _fail)

    assessment = await _assess(
        _observation(("health", True)),
        Observation(reached=False, note="unreachable"),
        model_provider=_Provider(),
    )

    assert not runner_used
    assert not assessment.passed
