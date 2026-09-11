"""C9-05: the evaluation harness, run over the labelled fixtures as CI does.

Two things this suite is really asserting.

First, that the numbers are *real*: the harness drives production code over real
git checkouts and scores what was recorded against what a person labelled, so a
regression in the differ or the correlator shows up here as a dropped
percentage rather than as a passing test.

Second, and less obviously, that a metric with no inputs says so. Zero out of
zero is not 0% and it is not 100%; it is a measurement that did not happen, and
a report that rendered it as a number would be inventing a result.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import SecretStr

from backend.evaluation.labels import LabelInvalid, load_case, load_cases
from backend.evaluation.metrics import SECTIONS
from backend.evaluation.report import evaluate, render
from backend.models.session import create_all
from backend.shared.config import Environment, Settings
from backend.shared.redaction import contains_secret

CASES = Path("tests/fixtures/labelled")

#: Every metric §18 names, by the name the catalogue uses. A metric that
#: disappears from the report is a metric nobody is measuring any more, and the
#: document would still claim it.
REQUIRED_METRICS = {
    "provider update detection latency",
    "missed updates",
    "breaking-change classification accuracy",
    "relevance accuracy",
    "unnecessary migration rate",
    "affected file accuracy",
    "affected workflow accuracy",
    "migration success rate",
    "repair iterations per success",
    "build success",
    "contract-test success",
    "regression-test success",
    "security violations",
    "unauthorized action attempts blocked",
    "correct approval escalation rate",
    "PR creation success",
    "total execution time",
    "tool calls",
    "token usage",
}


@pytest.fixture
def evaluation_settings(tmp_path: Path) -> Settings:
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'evaluation.db'}",
        SESSION_SECRET=SecretStr("evaluation-harness-secret-value"),
        _env_file=None,
    )


@pytest_asyncio.fixture(autouse=True)
async def database(evaluation_settings: Settings) -> AsyncIterator[None]:
    from backend.models.session import dispose_engine, init_engine

    init_engine(evaluation_settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


# --- The labelled set -----------------------------------------------------


def test_every_labelled_case_loads_and_is_coherent() -> None:
    case_set = load_cases(CASES)

    assert len(case_set) >= 4
    ids = [case.case_id for case in case_set.cases]
    assert len(set(ids)) == len(ids)


def test_the_set_contains_both_a_relevant_and_an_irrelevant_case() -> None:
    """Otherwise relevance accuracy is unfalsifiable.

    A set where every change is relevant is scored 100% by a system that always
    answers "relevant", which is the exact failure mode the selectivity of this
    product exists to avoid.
    """
    cases = load_cases(CASES).cases

    assert any(case.relevant for case in cases)
    assert any(not case.relevant for case in cases)
    assert any(change.breaking for case in cases for change in case.changes)
    assert any(not change.breaking for case in cases for change in case.changes)


def test_a_label_that_contradicts_its_own_fixture_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    (root / "repo").mkdir(parents=True)
    (root / "spec_before.json").write_text("{}")
    (root / "spec_after.json").write_text("{}")
    (root / "case.json").write_text(
        json.dumps(
            {
                "provider_id": "acmepay",
                "from_version": "v1",
                "to_version": "v2",
                "expected": {
                    "relevant": True,
                    "affected_files": ["app/does_not_exist.py"],
                },
            }
        )
    )

    with pytest.raises(LabelInvalid, match="not in the fixture repo"):
        load_case(root)


def test_a_case_expecting_a_migration_must_supply_one(tmp_path: Path) -> None:
    root = tmp_path / "no-migration"
    (root / "repo").mkdir(parents=True)
    (root / "spec_before.json").write_text("{}")
    (root / "spec_after.json").write_text("{}")
    (root / "case.json").write_text(
        json.dumps(
            {
                "provider_id": "acmepay",
                "from_version": "v1",
                "to_version": "v2",
                "expected": {"relevant": True, "migration_expected": True},
            }
        )
    )

    with pytest.raises(LabelInvalid, match="no migration/ directory"):
        load_case(root)


def test_the_fixtures_contain_no_credentials() -> None:
    """C9-05's security line, asserted rather than assumed."""
    for path in sorted(CASES.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        assert not contains_secret(text), f"{path} looks like it contains a secret"


# --- Running it -----------------------------------------------------------


async def test_the_harness_computes_every_metric_section_18_names() -> None:
    run = await evaluate(load_cases(CASES))

    names = {metric.name for metric in run.metrics.metrics}
    missing = REQUIRED_METRICS - names
    assert not missing, f"§18 names metrics the harness does not compute: {missing}"
    assert {metric.section for metric in run.metrics.metrics} <= set(SECTIONS)


async def test_the_deterministic_metrics_score_against_the_labels() -> None:
    """The numbers, not just their presence.

    These are the metrics code decides, so they are expected to be perfect on a
    set this small — and if the differ or the correlator regresses, this is
    where it shows up.
    """
    run = await evaluate(load_cases(CASES))
    scores = {metric.name: metric for metric in run.metrics.metrics}

    assert run.failed_cases == []
    assert scores["missed updates"].value == 0.0
    assert scores["breaking-change classification accuracy"].value == 100.0
    assert scores["relevance accuracy"].value == 100.0
    assert scores["affected file accuracy"].value == 100.0
    assert scores["relevance accuracy"].denominator == len(load_cases(CASES))


async def test_a_metric_with_no_inputs_is_unavailable_and_never_zero() -> None:
    """The difference between "nothing happened" and "we did not look"."""
    run = await evaluate(load_cases(CASES))
    scores = {metric.name: metric for metric in run.metrics.metrics}

    execution = scores["migration success rate"]
    assert not execution.available
    assert execution.value is None
    assert "no model provider configured" in execution.unavailable_reason

    tokens = scores["token usage"]
    assert not tokens.available
    assert "no model call reported a token count" in tokens.unavailable_reason


async def test_no_metric_is_a_model_opinion() -> None:
    """Every metric names a table, a label, or a measurement as its source."""
    run = await evaluate(load_cases(CASES))

    for metric in run.metrics.metrics:
        assert metric.source, f"{metric.name} does not say where it came from"
        assert "model" not in metric.source.lower() or "usage.py" in metric.source, (
            f"{metric.name} sources its value from a model: {metric.source}"
        )


async def test_the_health_score_is_published_with_its_formula() -> None:
    """§18: the formula travels with any displayed score."""
    run = await evaluate(load_cases(CASES))
    report = render(run)

    scored = [item for item in run.summary()["integration_health"] if item["available"]]
    assert scored, "the labelled projects are scannable, so they have a score"
    for item in scored:
        assert item["formula"], "a score without its formula is a number to trust blindly"
        assert item["inputs"], "every input must trace to a stored record"

    assert "Formula (as computed by backend/api/health_score.py)" in report
    assert "coverage_penalty" in report


async def test_the_report_says_what_it_could_not_measure() -> None:
    run = await evaluate(load_cases(CASES))
    report = render(run)

    assert "deterministic (no model)" in report
    assert "report as unavailable rather than being scored against a script" in report
    for section in SECTIONS:
        assert section in report


async def test_the_json_output_carries_every_metric_and_its_source() -> None:
    """What a CI job or a dashboard would consume."""
    run = await evaluate(load_cases(CASES))
    payload = json.loads(json.dumps(run.summary(), default=str))

    assert payload["cases"] == len(load_cases(CASES))
    assert payload["live"] is False
    names = {metric["name"] for metric in payload["metrics"]}
    assert REQUIRED_METRICS <= names
    for metric in payload["metrics"]:
        assert metric["source"]
        if metric["value"] is None:
            assert metric["unavailable_reason"]


async def test_one_broken_case_does_not_end_the_run(tmp_path: Path) -> None:
    """A harness that stops at the first failure reports nothing about the rest."""
    import shutil

    root = tmp_path / "cases"
    shutil.copytree(CASES, root)

    # An empty repository: `git commit` has nothing to commit, so materialising
    # this case fails where the others succeed.
    shutil.rmtree(root / "no-change" / "repo")
    (root / "no-change" / "repo").mkdir()

    run = await evaluate(load_cases(root))

    assert len(run.observations) == len(load_cases(root))
    (failed,) = run.failed_cases
    assert failed.case.case_id == "no-change"

    # The other cases still produced their metrics.
    scores = {metric.name: metric for metric in run.metrics.metrics}
    assert scores["breaking-change classification accuracy"].value == 100.0
    assert scores["relevance accuracy"].value == 100.0
