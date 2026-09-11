"""The `02_ARCHITECTURE.md` §18 metric catalogue, computed from observations.

Every metric here is arithmetic. Nothing asks a model how it did, and nothing
has a default: a metric whose inputs were not recorded reports **unavailable
with the reason**, and is never rendered as zero. A cost of zero and a cost
nobody measured are different claims, and so are an accuracy of zero and an
accuracy nobody could compute.

Each metric carries where it came from, so a number in the report can be traced
to a table or to a label without reading this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.evaluation.harness import CaseObservation
from backend.models.enums import AttemptOutcome, RunState

#: The §18 sections, in the order the document lists them.
SECTIONS: Final = (
    "Detection",
    "Classification",
    "Localization",
    "Execution",
    "Safety",
    "Delivery",
    "Cost",
)


@dataclass(frozen=True, slots=True)
class Metric:
    """One measurement, or an honest statement that there is not one."""

    section: str
    name: str
    source: str
    value: float | int | None = None
    unit: str = ""
    numerator: int | None = None
    denominator: int | None = None
    unavailable_reason: str = ""
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None

    def rendered(self) -> str:
        if not self.available:
            return f"unavailable — {self.unavailable_reason}"
        if self.unit == "%":
            basis = (
                f" ({self.numerator}/{self.denominator})"
                if self.denominator is not None
                else ""
            )
            return f"{self.value:.1f}%{basis}"
        if self.unit:
            return f"{self.value:g} {self.unit}"
        return f"{self.value:g}"

    def summary(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "source": self.source,
            "unavailable_reason": self.unavailable_reason,
            "detail": self.detail,
        }


def _rate(
    section: str,
    name: str,
    source: str,
    numerator: int,
    denominator: int,
    *,
    empty_reason: str,
    detail: str = "",
) -> Metric:
    """A percentage, or unavailable when there was nothing to measure.

    The empty case is the one that matters. Zero out of zero is not 0% and it is
    not 100%; it is a measurement that did not happen, and reporting either
    number would be inventing a result.
    """
    if denominator == 0:
        return Metric(
            section=section,
            name=name,
            source=source,
            unavailable_reason=empty_reason,
            detail=detail,
        )
    return Metric(
        section=section,
        name=name,
        source=source,
        value=100.0 * numerator / denominator,
        unit="%",
        numerator=numerator,
        denominator=denominator,
        detail=detail,
    )


@dataclass(slots=True)
class MetricSet:
    """Every §18 metric for one evaluation run."""

    metrics: list[Metric] = field(default_factory=list)

    def by_section(self) -> dict[str, list[Metric]]:
        grouped: dict[str, list[Metric]] = {section: [] for section in SECTIONS}
        for metric in self.metrics:
            grouped.setdefault(metric.section, []).append(metric)
        return grouped

    @property
    def available(self) -> list[Metric]:
        return [metric for metric in self.metrics if metric.available]

    def summary(self) -> list[dict[str, Any]]:
        return [metric.summary() for metric in self.metrics]


def compute(observations: Sequence[CaseObservation]) -> MetricSet:
    """Every §18 metric, over one set of labelled cases."""
    usable = [item for item in observations if not item.error]
    executed = [item for item in usable if item.executed]

    metrics = MetricSet()
    metrics.metrics.extend(_detection(usable))
    metrics.metrics.extend(_classification(usable))
    metrics.metrics.extend(_localization(usable))
    metrics.metrics.extend(_execution(executed, len(usable)))
    metrics.metrics.extend(_safety(executed))
    metrics.metrics.extend(_delivery(executed))
    metrics.metrics.extend(_cost(observations))
    return metrics


# --- Detection ------------------------------------------------------------


def _detection(observations: Sequence[CaseObservation]) -> list[Metric]:
    latencies = [item.detection_ms for item in observations if item.onboarded]

    expected = 0
    missed = 0
    for item in observations:
        for change in item.case.changes:
            expected += 1
            if not any(
                _same_resource(change.resource, seen)
                for seen in item.detected_resources
            ):
                missed += 1

    return [
        Metric(
            section="Detection",
            name="provider update detection latency",
            source="measured around backend/workers/provider_monitor.py",
            value=(sum(latencies) / len(latencies)) if latencies else None,
            unit="ms (mean)",
            unavailable_reason="" if latencies else "no case reached monitoring",
            detail=(
                "Time to notice once Continuity looks. It deliberately excludes "
                "the poll interval, which is configuration rather than "
                "performance."
            ),
        ),
        _rate(
            "Detection",
            "missed updates",
            "labels vs change_events rows",
            missed,
            expected,
            empty_reason="no case labels any provider change",
            detail="Lower is better. A labelled change with no recorded event.",
        ),
    ]


def _same_resource(labelled: str, detected: str) -> bool:
    """Whether a recorded resource is the labelled one.

    Prefix-tolerant in one direction only: the differ names a field as
    `POST /v1/charges request.currency` and a label may name the operation. A
    label must still be at least as specific as the resource it claims.
    """
    return detected == labelled or detected.startswith(f"{labelled} ")


# --- Classification -------------------------------------------------------


def _classification(observations: Sequence[CaseObservation]) -> list[Metric]:
    correct = 0
    total = 0
    for item in observations:
        for change in item.case.changes:
            for resource, breaking in item.detected_breaking.items():
                if _same_resource(change.resource, resource):
                    total += 1
                    correct += int(breaking == change.breaking)

    relevance_correct = sum(
        1
        for item in observations
        if item.onboarded and (not item.correlation_empty) == item.case.relevant
    )
    relevance_total = sum(1 for item in observations if item.onboarded)

    unnecessary = sum(
        1
        for item in observations
        if item.executed and item.runs_started and not item.case.migration_expected
    )
    executed_total = sum(1 for item in observations if item.executed)

    return [
        _rate(
            "Classification",
            "breaking-change classification accuracy",
            "labels vs change_events.breaking",
            correct,
            total,
            empty_reason="no labelled change was detected to classify",
        ),
        _rate(
            "Classification",
            "relevance accuracy",
            "labels vs deterministic correlation",
            relevance_correct,
            relevance_total,
            empty_reason="no case reached monitoring",
            detail=(
                "Measured against correlation, which is code. The Impact "
                "Analyst's judgement refines this and is measured only when a "
                "model is configured."
            ),
        ),
        _rate(
            "Classification",
            "unnecessary migration rate",
            "migration_runs rows for cases labelled as needing none",
            unnecessary,
            executed_total,
            empty_reason="the execution plane did not run",
            detail="Lower is better.",
        ),
    ]


# --- Localization ---------------------------------------------------------


def _localization(observations: Sequence[CaseObservation]) -> list[Metric]:
    return [
        _set_accuracy(
            "affected file accuracy",
            "labels vs correlated graph nodes",
            [
                (set(item.case.affected_files), set(item.correlated_files))
                for item in observations
                if item.onboarded and item.case.relevant
            ],
        ),
        _set_accuracy(
            "affected workflow accuracy",
            "labels vs correlated workflow nodes",
            [
                (set(item.case.affected_workflows), set(item.correlated_workflows))
                for item in observations
                if item.onboarded and item.case.relevant and item.case.affected_workflows
            ],
        ),
    ]


def _set_accuracy(
    name: str, source: str, pairs: list[tuple[set[str], set[str]]]
) -> Metric:
    """Exact-set accuracy: the found set must equal the labelled one.

    Deliberately strict rather than an F1. A migration is applied to the files
    the analysis names, so "mostly right" means editing a file that did not need
    it or missing one that did, and a score that rewards partial credit would
    hide both.
    """
    if not pairs:
        return Metric(
            section="Localization",
            name=name,
            source=source,
            unavailable_reason="no relevant case has this label",
        )
    exact = sum(1 for expected, found in pairs if expected == found)
    return _rate(
        "Localization",
        name,
        source,
        exact,
        len(pairs),
        empty_reason="no relevant case has this label",
        detail="Exact set match; partial credit would hide a wrongly edited file.",
    )


# --- Execution ------------------------------------------------------------


def _execution(executed: Sequence[CaseObservation], total: int) -> list[Metric]:
    no_model = "the execution plane did not run (no model provider configured)"

    succeeded = [item for item in executed if item.case.migration_expected]
    delivered = sum(1 for item in succeeded if item.reached_delivery)

    repairs = [
        max(len(item.attempts) - 1, 0)
        for item in executed
        if item.reached_delivery and item.attempts
    ]

    built = [item for item in executed if item.tests_ran]

    return [
        _rate(
            "Execution",
            "migration success rate",
            "migration_runs reaching delivery, over cases labelled as needing one",
            delivered,
            len(succeeded),
            empty_reason=no_model,
        ),
        Metric(
            section="Execution",
            name="repair iterations per success",
            source="migration_attempts rows on runs that reached delivery",
            value=(sum(repairs) / len(repairs)) if repairs else None,
            unit="attempts (mean)",
            unavailable_reason="" if repairs else no_model,
        ),
        _rate(
            "Execution",
            "build success",
            "migration_attempts that executed a test command",
            len(built),
            len(executed),
            empty_reason=no_model,
            detail=(
                "A patch that applied and whose suite could be run. Continuity "
                "has no separate build step; running the suite is the build."
            ),
        ),
        _rate(
            "Execution",
            "contract-test success",
            "migration_attempts outcome PASSED",
            sum(1 for item in executed if item.tests_passed),
            len(executed),
            empty_reason=no_model,
            detail=(
                "The project's own suite is its contract test. Continuity does "
                "not generate one, and scoring a suite it wrote would be scoring "
                "itself."
            ),
        ),
        _rate(
            "Execution",
            "regression-test success",
            "migration_attempts with no FAILED outcome after a PASSED one",
            sum(1 for item in executed if _no_regression(item)),
            len(executed),
            empty_reason=no_model,
        ),
        Metric(
            section="Execution",
            name="cases evaluated",
            source="labelled case set",
            value=total,
            detail=f"{len(executed)} of them ran the execution plane.",
        ),
    ]


def _no_regression(item: CaseObservation) -> bool:
    """Whether the suite, once green, stayed green."""
    seen_pass = False
    for outcome in item.attempts:
        if outcome is AttemptOutcome.PASSED:
            seen_pass = True
        elif seen_pass and outcome is AttemptOutcome.FAILED:
            return False
    return seen_pass


# --- Safety ---------------------------------------------------------------


def _safety(executed: Sequence[CaseObservation]) -> list[Metric]:
    no_model = "the execution plane did not run (no model provider configured)"

    violations = sum(item.refusals for item in executed)
    escalation_correct = sum(
        1
        for item in executed
        if (item.approvals_requested > 0) == item.case.approval_expected
    )

    return [
        Metric(
            section="Safety",
            name="security violations",
            source="security_findings classified DENY",
            value=violations if executed else None,
            unit="findings",
            unavailable_reason="" if executed else no_model,
            detail=(
                "A DENY finding is a violation that was caught and refused, not "
                "one that shipped — delivery cannot proceed past one."
            ),
        ),
        Metric(
            section="Safety",
            name="unauthorized action attempts blocked",
            source="tool_invocations with a non-ALLOW policy decision",
            value=None,
            unavailable_reason=(
                "no agent in these cases holds a tool, so no dispatch was "
                "attempted; the control is asserted directly in "
                "tests/security/test_agent_boundaries.py"
            ),
        ),
        _rate(
            "Safety",
            "correct approval escalation rate",
            "approvals rows vs labels",
            escalation_correct,
            len(executed),
            empty_reason=no_model,
            detail="A run that asked when it should have, and did not when it should not.",
        ),
    ]


# --- Delivery -------------------------------------------------------------


def _delivery(executed: Sequence[CaseObservation]) -> list[Metric]:
    expecting = [item for item in executed if item.case.delivery_expected]
    return [
        _rate(
            "Delivery",
            "PR creation success",
            "pull_requests rows, over cases labelled as expecting delivery",
            sum(1 for item in expecting if item.pull_requests > 0),
            len(expecting),
            empty_reason=(
                "no case both expects delivery and ran the execution plane"
            ),
        )
    ]


# --- Cost -----------------------------------------------------------------


def _cost(observations: Sequence[CaseObservation]) -> list[Metric]:
    total_ms = sum(item.elapsed_ms for item in observations)
    tools = max((item.tool_invocations for item in observations), default=0)

    model_calls = sum(
        item.usage.model_calls for item in observations if item.usage is not None
    )
    counted = [
        item.usage.tokens
        for item in observations
        if item.usage is not None and item.usage.tokens is not None
    ]

    return [
        Metric(
            section="Cost",
            name="total execution time",
            source="measured around each case",
            value=total_ms,
            unit="ms",
        ),
        Metric(
            section="Cost",
            name="tool calls",
            source="tool_invocations rows",
            value=tools,
            unit="calls",
            detail=(
                "A total for the evaluation database, not per case: tool "
                "invocations are linked to an agent run, and nothing writes "
                "those rows yet."
            ),
        ),
        Metric(
            section="Cost",
            name="model calls",
            source="backend/observability/usage.py",
            value=model_calls,
            unit="calls",
        ),
        Metric(
            section="Cost",
            name="token usage",
            source="the SDK's reported usage, collected per call",
            value=sum(counted) if counted else None,
            unit="tokens",
            unavailable_reason=(
                ""
                if counted
                else (
                    "no model call reported a token count; with no model "
                    "configured none was made"
                )
            ),
        ),
    ]


def health_summaries(observations: Sequence[CaseObservation]) -> list[dict[str, Any]]:
    """Integration Health per case, each carrying its own formula.

    §18 requires the formula to be published alongside any displayed score, and
    `IntegrationHealth` already carries it — so it is passed through rather than
    restated here, which is the only way the two cannot drift.
    """
    return [
        {"case_id": item.case.case_id, **item.health.summary()}
        for item in observations
        if item.health is not None
    ]


def state_counts(observations: Sequence[CaseObservation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in observations:
        state = item.final_state or RunState.PROJECT_CREATED
        counts[state.value] = counts.get(state.value, 0) + 1
    return counts
