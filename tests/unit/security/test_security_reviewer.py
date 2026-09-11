"""C8-01: reviewing the generated diff.

The acceptance is one fixture diff per `FindingCategory`, and it is checked by
parametrizing over the enum rather than over a hand-written list — a member
added later fails this module until someone decides what it costs.

The agent is stubbed. What is under test is the division of authority: code
finds what it can see, the model adds what it read, and neither of them decides.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.agents import security_reviewer as reviewer_module
from backend.agents.contracts import ProposedFinding, SecurityReviewerOutput
from backend.agents.security_reviewer import (
    CATEGORY_ACTION,
    MAX_DIFF_CHARS,
    classify_category,
    review_patch,
)
from backend.models.enums import (
    Confidence,
    EvidenceKind,
    FindingCategory,
    PolicyDecision,
    Severity,
)
from backend.models.schemas import Evidence
from backend.security.categories import changed_paths, detect
from tests.support.diff_fixtures import CLEAN, FIXTURES, VERIFICATION_DISABLED

ALL_CATEGORIES = sorted(FindingCategory, key=lambda c: c.value)


class StubProvider:
    @property
    def model_id(self) -> str:
        return "stub"

    def build_model(self, role: Any) -> Any:
        return object()


class StubRunner:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0
        self.prompts: list[str] = []

    async def run_structured(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.prompts.append(str(kwargs.get("prompt", "")))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def patched_agent(monkeypatch: pytest.MonkeyPatch):
    def install(outcome: Any) -> StubRunner:
        runner = StubRunner(outcome)
        original = reviewer_module.SecurityReviewerAgent

        class Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, provider: Any, **kwargs: Any) -> None:
                super().__init__(provider, runner=runner, **kwargs)

        monkeypatch.setattr(reviewer_module, "SecurityReviewerAgent", Patched)
        return runner

    return install


async def _review(diff: str, **kwargs: Any):
    return await review_patch(
        provider_id="acmepay",
        diff=diff,
        changed_dependencies=kwargs.pop("changed_dependencies", []),
        modifies_tests=kwargs.pop("modifies_tests", False),
        **kwargs,
    )


# --- one fixture per category -------------------------------------------


def test_every_finding_category_has_a_fixture() -> None:
    """Guards the parametrized test below against covering less than all 13."""
    assert set(FIXTURES) == set(FindingCategory)
    assert len(FIXTURES) == 13


@pytest.mark.parametrize("category", ALL_CATEGORIES, ids=lambda c: c.value)
async def test_each_category_is_produced_by_its_fixture_diff(
    category: FindingCategory,
) -> None:
    """C8-01 acceptance, one member at a time.

    Deterministic, so no model is involved: these are the categories code can
    decide, and the security floor must not depend on a model being reachable.
    """
    diff, impact_set = FIXTURES[category]

    review = await _review(diff, impact_set=impact_set)

    assert category in {finding.category for finding in review.findings}


@pytest.mark.parametrize("category", ALL_CATEGORIES, ids=lambda c: c.value)
async def test_every_finding_carries_evidence(category: FindingCategory) -> None:
    """C8-01 acceptance. A finding a reviewer cannot locate is one they ignore."""
    diff, impact_set = FIXTURES[category]

    review = await _review(diff, impact_set=impact_set)
    finding = next(f for f in review.findings if f.category is category)

    assert isinstance(finding.evidence, Evidence)
    assert finding.evidence.excerpt
    assert finding.evidence.confidence is Confidence.CONFIRMED
    assert finding.evidence.kind is EvidenceKind.SOURCE


@pytest.mark.parametrize("category", ALL_CATEGORIES, ids=lambda c: c.value)
def test_every_category_maps_to_a_policy_action(category: FindingCategory) -> None:
    """A category with no mapping would be classified DENY by the backstop.

    That is the right default, and it is also a bug: nobody decided what this
    category costs. Asserting the mapping is complete makes that a failing test
    instead of a surprise refusal in production.
    """
    assert category in CATEGORY_ACTION
    assert classify_category(category) in set(PolicyDecision)


def test_a_clean_patch_produces_no_findings() -> None:
    """The detectors have to be quiet on ordinary work.

    A reviewer that fires on every migration is one nobody reads, and the
    fixture here is exactly the patch Phase 7's engineer produces.
    """
    assert detect(CLEAN, impact_set=["app/client.py"]) == []


async def test_a_clean_patch_passes_review() -> None:
    review = await _review(CLEAN, impact_set=["app/client.py"])

    assert review.passed
    assert review.decision is PolicyDecision.ALLOW
    assert review.findings == []
    assert "No security findings" in review.summary


# --- what the decisions actually are -------------------------------------


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (FindingCategory.SECRET_EXPOSURE, PolicyDecision.DENY),
        (FindingCategory.AUTHORIZATION_WEAKENED, PolicyDecision.DENY),
        (FindingCategory.WEBHOOK_VERIFICATION, PolicyDecision.DENY),
        (FindingCategory.NEW_DEPENDENCY, PolicyDecision.ASK),
        (FindingCategory.TEST_WEAKENED, PolicyDecision.ASK),
        (FindingCategory.OAUTH_SCOPE_CHANGE, PolicyDecision.ASK),
        (FindingCategory.AUTHENTICATION_CHANGE, PolicyDecision.ASK),
    ],
)
def test_categories_carry_the_decision_they_should(
    category: FindingCategory, expected: PolicyDecision
) -> None:
    """Spot-checked deliberately.

    A credential or a removed security check is never a question for a human —
    it is a refusal. A new dependency or a changed scope is exactly a question
    for a human. Getting these two groups the wrong way round is the failure
    that would matter most.
    """
    assert classify_category(category) is expected


async def test_the_strictest_finding_decides_the_review() -> None:
    """One DENY outranks any number of ASKs."""
    combined = FIXTURES[FindingCategory.NEW_DEPENDENCY][0] + FIXTURES[
        FindingCategory.SECRET_EXPOSURE
    ][0]

    review = await _review(combined)

    assert review.decision is PolicyDecision.DENY
    assert not review.passed
    assert len(review.blocking) >= 2


async def test_verification_turned_off_is_caught_as_well_as_removed() -> None:
    """`verify=False` leaves the call site looking intact.

    Removing the check is the obvious shape; disabling it in place is the one
    that survives a casual read of the diff.
    """
    review = await _review(VERIFICATION_DISABLED)

    categories = {finding.category for finding in review.findings}
    assert FindingCategory.WEBHOOK_VERIFICATION in categories
    assert review.decision is PolicyDecision.DENY


async def test_a_secret_is_never_quoted_in_the_finding() -> None:
    """The finding is stored, reported, and put in a pull request body."""
    from tests.support.secret_samples import GITHUB_TOKEN

    review = await _review(FIXTURES[FindingCategory.SECRET_EXPOSURE][0])
    finding = next(
        f for f in review.findings if f.category is FindingCategory.SECRET_EXPOSURE
    )

    assert GITHUB_TOKEN not in finding.summary
    assert GITHUB_TOKEN not in str(finding.evidence.model_dump())
    assert GITHUB_TOKEN not in str(review.report())


# --- the agent recommends; policy decides --------------------------------


async def test_a_disagreement_is_recorded_rather_than_resolved(
    patched_agent: Any,
) -> None:
    """C8-01 acceptance, and the reason both columns exist.

    The agent says a credential in the patch is fine. It is not, and the review
    refuses — but the agent's view is stored beside the refusal, because a
    persistent gap between the two is worth investigating rather than smoothing
    over (`03_SECURITY_ACCESS.md` §10).
    """
    patched_agent(
        SecurityReviewerOutput(
            findings=[
                ProposedFinding(
                    category=FindingCategory.AUTHORIZATION_WEAKENED,
                    severity=Severity.LOW,
                    summary="The removed check was redundant.",
                    evidence=Evidence(
                        kind=EvidenceKind.SOURCE,
                        confidence=Confidence.INFERRED,
                        file_path="app/views.py",
                        excerpt="pass",
                    ),
                    recommendation=PolicyDecision.ALLOW,
                )
            ],
            overall_recommendation=PolicyDecision.ALLOW,
            summary="Nothing here concerns me.",
        )
    )

    review = await _review(CLEAN, impact_set=["app/client.py"], model_provider=StubProvider())

    (finding,) = review.findings
    assert finding.category is FindingCategory.AUTHORIZATION_WEAKENED
    assert finding.recommendation is PolicyDecision.ALLOW
    assert finding.policy_decision is PolicyDecision.DENY
    assert finding.disagreed
    assert review.disagreements == [finding]

    # The policy engine wins, and the run does not proceed.
    assert review.decision is PolicyDecision.DENY
    assert review.agent_recommendation is PolicyDecision.ALLOW
    assert review.report()["disagreements"] == 1


async def test_the_agent_cannot_clear_a_finding_code_already_made(
    patched_agent: Any,
) -> None:
    """The deterministic floor does not move.

    An agent that reported "no findings" on a patch containing a credential
    must not be able to talk the review into passing.
    """
    patched_agent(
        SecurityReviewerOutput(
            findings=[],
            overall_recommendation=PolicyDecision.ALLOW,
            summary="Looks clean to me.",
        )
    )

    review = await _review(
        FIXTURES[FindingCategory.SECRET_EXPOSURE][0], model_provider=StubProvider()
    )

    assert not review.passed
    assert review.decision is PolicyDecision.DENY
    assert review.agent_recommendation is PolicyDecision.ALLOW


async def test_the_agent_adds_categories_code_did_not_find(patched_agent: Any) -> None:
    """This is what the model is for.

    A diff can be wrong in ways no pattern catches, and a finding the agent
    contributes is marked INFERRED so a reader can tell it apart from one a
    pattern matched.
    """
    patched_agent(
        SecurityReviewerOutput(
            findings=[
                ProposedFinding(
                    category=FindingCategory.DUPLICATE_TRANSACTION_RISK,
                    severity=Severity.HIGH,
                    summary="The new code path can charge twice on a timeout.",
                    evidence=Evidence(
                        kind=EvidenceKind.SOURCE,
                        confidence=Confidence.INFERRED,
                        file_path="app/client.py",
                        excerpt="retry without an idempotency key",
                    ),
                    recommendation=PolicyDecision.ASK,
                )
            ],
            overall_recommendation=PolicyDecision.ASK,
            summary="One risk worth a look.",
        )
    )

    review = await _review(CLEAN, impact_set=["app/client.py"], model_provider=StubProvider())

    (finding,) = review.findings
    assert finding.source == "security_reviewer"
    assert finding.evidence.confidence is Confidence.INFERRED
    assert not finding.disagreed
    assert review.decision is PolicyDecision.ASK


async def test_the_agent_does_not_duplicate_a_deterministic_finding(
    patched_agent: Any,
) -> None:
    """Code already quoted the line; the model's version would double-count."""
    patched_agent(
        SecurityReviewerOutput(
            findings=[
                ProposedFinding(
                    category=FindingCategory.SECRET_EXPOSURE,
                    severity=Severity.CRITICAL,
                    summary="There is a token in this patch.",
                    evidence=Evidence(
                        kind=EvidenceKind.SOURCE,
                        confidence=Confidence.INFERRED,
                        file_path="app/client.py",
                    ),
                    recommendation=PolicyDecision.DENY,
                )
            ],
            overall_recommendation=PolicyDecision.DENY,
            summary="Credential found.",
        )
    )

    review = await _review(
        FIXTURES[FindingCategory.SECRET_EXPOSURE][0], model_provider=StubProvider()
    )

    secrets = [
        f for f in review.findings if f.category is FindingCategory.SECRET_EXPOSURE
    ]
    assert len(secrets) == 1
    assert secrets[0].source == "deterministic"


async def test_a_failed_agent_leaves_the_deterministic_findings_standing(
    patched_agent: Any,
) -> None:
    """Losing the model degrades the review; it does not empty it.

    Reporting "nothing found" because the reviewer was unreachable would be a
    security claim nobody made.
    """
    patched_agent(RuntimeError("model unavailable"))

    review = await _review(
        FIXTURES[FindingCategory.SECRET_EXPOSURE][0], model_provider=StubProvider()
    )

    assert not review.passed
    assert review.decision is PolicyDecision.DENY
    assert not review.model_consulted
    assert "only the deterministic checks" in review.summary


async def test_a_review_without_a_model_still_happens() -> None:
    """The floor does not depend on a model being configured at all."""
    review = await _review(FIXTURES[FindingCategory.SECRET_EXPOSURE][0])

    assert not review.passed
    assert not review.model_consulted
    assert review.agent_recommendation is review.decision


async def test_an_enormous_diff_is_reviewed_and_marked_truncated(
    patched_agent: Any,
) -> None:
    """The model sees a bounded diff, and the report says so.

    Quietly truncating would let a reviewer believe the whole patch was read.
    """
    patched_agent(
        SecurityReviewerOutput(findings=[], overall_recommendation=PolicyDecision.ALLOW, summary="ok")
    )
    runner_diff = CLEAN + ("\n+# padding" * 40_000)

    review = await _review(
        runner_diff, impact_set=["app/client.py"], model_provider=StubProvider()
    )

    assert review.diff_truncated
    assert len(runner_diff) > MAX_DIFF_CHARS
    assert review.report()["diff_truncated"] is True


# --- diff parsing --------------------------------------------------------


def test_changed_paths_are_read_from_the_diff_headers() -> None:
    combined = FIXTURES[FindingCategory.NEW_DEPENDENCY][0] + FIXTURES[
        FindingCategory.TEST_WEAKENED
    ][0]

    assert changed_paths(combined) == ["pyproject.toml", "tests/test_client.py"]


def test_an_empty_diff_produces_nothing() -> None:
    assert detect("") == []
    assert detect("   \n  ") == []


def test_the_file_header_is_not_mistaken_for_an_added_line() -> None:
    """`+++ b/path` starts with `+` and is not code.

    Treating it as an added line would make every diff touching a file whose
    name contains a pattern word into a finding.
    """
    diff = FIXTURES[FindingCategory.SECRET_EXPOSURE][0]

    from backend.security.categories import added_lines

    assert not any(line.text.startswith("++") for line in added_lines(diff))
    assert all(line.path == "app/client.py" for line in added_lines(diff))
