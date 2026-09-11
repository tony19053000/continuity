"""Rendering an evaluation run, and running one.

Two outputs from one computation: text for a person and JSON for a machine. The
text is what a reviewer reads and so it states, at the top, what this run could
not measure — a report whose caveats are in a footnote is a report whose caveats
do not exist.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from backend.evaluation.harness import CaseObservation, run_case
from backend.evaluation.labels import CaseSet, load_cases
from backend.evaluation.metrics import (
    SECTIONS,
    MetricSet,
    compute,
    health_summaries,
    state_counts,
)
from backend.models.session import session_scope
from backend.observability.logging import get_logger
from backend.shared.model_provider import ModelProvider

logger = get_logger(__name__)


@dataclass(slots=True)
class EvaluationRun:
    """One pass over a labelled set."""

    case_set_root: Path
    observations: list[CaseObservation] = field(default_factory=list)
    metrics: MetricSet = field(default_factory=MetricSet)
    live: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def failed_cases(self) -> list[CaseObservation]:
        return [item for item in self.observations if item.error]

    def summary(self) -> dict[str, Any]:
        return {
            "case_set": str(self.case_set_root),
            "started_at": self.started_at.isoformat(),
            "live": self.live,
            "cases": len(self.observations),
            "failed_cases": [
                {"case_id": item.case.case_id, "error": item.error}
                for item in self.failed_cases
            ],
            "metrics": self.metrics.summary(),
            "integration_health": health_summaries(self.observations),
            "final_states": state_counts(self.observations),
            "per_case": [item.summary() for item in self.observations],
        }


async def evaluate(
    case_set: CaseSet,
    *,
    model_provider: ModelProvider | None = None,
    max_attempts: int = 3,
) -> EvaluationRun:
    """Run every case and compute the metrics."""
    run = EvaluationRun(case_set_root=case_set.root, live=model_provider is not None)

    with TemporaryDirectory(prefix="continuity-evaluation-") as scratch:
        root = Path(scratch)
        checkouts = root / "checkouts"
        workspaces = root / "workspaces"
        checkouts.mkdir()
        workspaces.mkdir()

        for case in case_set.cases:
            # One session per case. A case that leaves its transaction in a bad
            # state must not take the rest of the set with it.
            async with session_scope() as session:
                run.observations.append(
                    await run_case(
                        session,
                        case,
                        workspace_root=workspaces,
                        checkout_root=checkouts,
                        model_provider=model_provider,
                        max_attempts=max_attempts,
                    )
                )

    run.metrics = compute(run.observations)
    logger.info(
        "continuity.evaluation_complete",
        extra={
            "cases": len(run.observations),
            "failed": len(run.failed_cases),
            "metrics": len(run.metrics.metrics),
            "available": len(run.metrics.available),
            "live": run.live,
        },
    )
    return run


def render(run: EvaluationRun) -> str:
    """The report a person reads."""
    lines: list[str] = [
        "Continuity evaluation",
        "=" * 60,
        f"case set   {run.case_set_root}",
        f"cases      {len(run.observations)}",
        f"mode       {'live (a model is configured)' if run.live else 'deterministic (no model)'}",
        "",
    ]

    if not run.live:
        lines += [
            "This run measured the half of Continuity that code decides: detection,",
            "breaking-change classification, correlation-based relevance and",
            "localisation, and Integration Health. The execution, safety, and",
            "delivery metrics need a Migration Engineer, which is a model — they",
            "report as unavailable rather than being scored against a script.",
            "",
        ]

    if run.failed_cases:
        lines.append("Cases that did not complete:")
        lines += [
            f"  {item.case.case_id}: {item.error}" for item in run.failed_cases
        ]
        lines.append("")

    grouped = run.metrics.by_section()
    for section in SECTIONS:
        metrics = grouped.get(section, [])
        if not metrics:
            continue
        lines.append(section)
        lines.append("-" * len(section))
        width = max(len(metric.name) for metric in metrics)
        for metric in metrics:
            lines.append(f"  {metric.name.ljust(width)}  {metric.rendered()}")
            if metric.detail:
                lines.append(f"  {' ' * width}  ({metric.detail})")
        lines.append("")

    healths = health_summaries(run.observations)
    scored = [item for item in healths if item.get("available")]
    lines.append("Integration Health")
    lines.append("-" * len("Integration Health"))
    if not scored:
        lines.append("  No case has a score. Every input must be a stored record.")
        for item in healths:
            lines.append(f"  {item['case_id']}: {item.get('unavailable_reason', '')}")
    else:
        for item in scored:
            lines.append(f"  {item['case_id']}: {item['score']}")
        # §18 requires the formula beside any displayed score, and this is the
        # formula the code used, not a copy of it.
        lines.append("")
        lines.append("  Formula (as computed by backend/api/health_score.py):")
        lines += [f"    {row}" for row in str(scored[0]["formula"]).splitlines()]
    lines.append("")

    return "\n".join(lines)


async def run_evaluation(
    root: Path,
    *,
    model_provider: ModelProvider | None = None,
    json_path: Path | None = None,
) -> EvaluationRun:
    """Load, run, report. The whole command, in one call."""
    run = await evaluate(load_cases(root), model_provider=model_provider)
    if json_path is not None:
        payload = json.dumps(run.summary(), indent=2, default=str)
        await asyncio.to_thread(json_path.write_text, payload)
    return run
