"""C6-03: decide whether a provider change actually matters *here*.

Most provider changes are irrelevant to any given codebase. Saying so is the
product's most frequent correct answer, and the one that makes autonomous
monitoring tolerable — an unnecessary migration run costs a review cycle and
erodes trust in every alert after it.

Two decisions, made in two different places on purpose:

* **Does this reach any code at all?** Answered by C6-02, deterministically. A
  change correlating to nothing is irrelevant, and **no model is invoked for
  it.** That is not an optimisation, it is the division of labour: there is no
  judgment to make about a change that touches no line of this repository.
* **Given that it reaches code, does it break anything?** That is judgment, and
  it is the agent's.

*What* is affected is computed from the graph rather than taken from the agent.
Every affected file, symbol, workflow, and test therefore carries the CONFIRMED
evidence the extractor recorded, and a path the model invented cannot reach a
report — it is dropped and counted, the same way a hallucinated symbol key is
dropped in C4-03.

This module holds the **judgment** and nothing else. Creating a migration run
and moving a project's state live in `backend/orchestration/impact.py`, because
only the coordinator moves runs (`02_ARCHITECTURE.md` §12) — a rule enforced by
`tests/security/test_agent_boundaries.py`, which forbids any module under
`backend/agents/` from importing the state machine at all. An earlier version of
this file did both, and that test is what caught it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from backend.agents.contracts import (
    AnalyzedChange,
    ImpactAnalystInput,
    ImpactAnalystOutput,
)
from backend.agents.specialists import ImpactAnalystAgent
from backend.integrations.correlation import Correlation
from backend.models.enums import Confidence, Severity
from backend.models.schemas import Evidence
from backend.observability.logging import get_logger
from backend.shared.model_provider import ModelProvider

logger = get_logger(__name__)

#: How much source context reaches the model per change. A change is judged from
#: the call sites it touches, not from the repository.
MAX_SOURCE_SLICES = 12


@dataclass(frozen=True, slots=True)
class AffectedItem:
    """One thing a change reaches, with the evidence that says so."""

    key: str
    label: str
    evidence: Evidence | None

    def summary(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "evidence": self.evidence.model_dump(mode="json") if self.evidence else None,
        }


@dataclass(slots=True)
class ImpactAssessment:
    """What one change means for one project."""

    change_event_id: uuid.UUID
    relevant: bool
    severity: Severity
    migration_required: bool
    reasoning_summary: str
    #: `correlation` when code decided, `impact_analyst` when the model did.
    decided_by: str
    affected_files: list[AffectedItem] = field(default_factory=list)
    affected_symbols: list[AffectedItem] = field(default_factory=list)
    affected_workflows: list[AffectedItem] = field(default_factory=list)
    affected_tests: list[AffectedItem] = field(default_factory=list)
    authentication_consequence: str | None = None
    #: Paths the agent named that the graph does not contain.
    dropped_unknown: list[str] = field(default_factory=list)
    migration_run_id: uuid.UUID | None = None

    @property
    def affected_anything(self) -> bool:
        return bool(
            self.affected_files
            or self.affected_symbols
            or self.affected_workflows
            or self.affected_tests
        )

    def summary(self) -> dict[str, object]:
        return {
            "relevant": self.relevant,
            "severity": self.severity.value,
            "migration_required": self.migration_required,
            "decided_by": self.decided_by,
            "affected_files": [item.summary() for item in self.affected_files],
            "affected_symbols": [item.summary() for item in self.affected_symbols],
            "affected_workflows": [item.summary() for item in self.affected_workflows],
            "affected_tests": [item.summary() for item in self.affected_tests],
            "authentication_consequence": self.authentication_consequence,
            "reasoning_summary": self.reasoning_summary,
            "dropped_unknown": list(self.dropped_unknown),
        }


async def assess_change(
    correlation: Correlation,
    change_event_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
    model_provider: ModelProvider | None = None,
    source_slices: list[str] | None = None,
) -> tuple[ImpactAssessment, bool]:
    """Assess one change. Returns the assessment and whether a model ran.

    The deterministic answer comes first and is final when it applies: a change
    correlating to no call site, no webhook handler, and no permission cannot
    affect this project, and asking a model would invite it to find impact that
    is not there.
    """
    if correlation.is_empty:
        return (
            ImpactAssessment(
                change_event_id=change_event_id,
                relevant=False,
                severity=Severity.INFO,
                migration_required=False,
                reasoning_summary=(
                    f"{correlation.change.change_type.value} on "
                    f"{correlation.change.resource} correlates to no call site, "
                    "webhook handler, or declared permission in this project."
                ),
                decided_by="correlation",
            ),
            False,
        )

    blast = correlation.blast
    affected_files = _items(blast.files if blast else [])
    affected_symbols = _items(blast.symbols if blast else [])
    affected_workflows = _items(blast.workflows if blast else [])
    affected_tests = _items(blast.tests if blast else [])

    if model_provider is None:
        # Correlated, but no model available to judge it. Reported as relevant
        # and escalated rather than quietly dropped: code that calls a changed
        # endpoint is a fact, and the absence of a judge is not evidence of
        # safety.
        return (
            ImpactAssessment(
                change_event_id=change_event_id,
                relevant=True,
                severity=Severity.MEDIUM,
                migration_required=False,
                reasoning_summary=(
                    "Correlated to code, but no model was available to judge "
                    "severity. Escalated for human review rather than assumed safe."
                ),
                decided_by="correlation",
                affected_files=affected_files,
                affected_symbols=affected_symbols,
                affected_workflows=affected_workflows,
                affected_tests=affected_tests,
            ),
            False,
        )

    agent = ImpactAnalystAgent(model_provider)
    output: ImpactAnalystOutput = await agent.run(
        ImpactAnalystInput(
            project_id=str(project_id),
            change=_analyzed(correlation),
            correlated_call_sites=[node.key for node in correlation.call_sites]
            + [node.key for node in correlation.webhook_handlers],
            correlated_workflows=[item.label for item in affected_workflows],
            relevant_source_slices=(source_slices or [])[:MAX_SOURCE_SLICES],
        )
    )

    known = {item.key for item in affected_files} | {
        item.label for item in affected_files
    }
    dropped = sorted(
        {
            path
            for path in output.affected_files
            if path not in known and path not in {item.key for item in affected_symbols}
        }
    )
    if dropped:
        logger.info(
            "continuity.impact_named_unknown_paths",
            extra={"count": len(dropped), "resource": correlation.change.resource},
        )

    return (
        ImpactAssessment(
            change_event_id=change_event_id,
            relevant=output.relevant,
            severity=output.severity,
            # A migration is only required if the change is relevant. The
            # conjunction is code's, not the model's: "irrelevant but migrate"
            # is not a coherent answer and must not be able to open a run.
            migration_required=output.relevant and output.migration_required,
            reasoning_summary=output.reasoning_summary,
            decided_by="impact_analyst",
            affected_files=affected_files,
            affected_symbols=affected_symbols,
            affected_workflows=affected_workflows,
            affected_tests=affected_tests,
            authentication_consequence=output.authentication_consequence,
            dropped_unknown=dropped,
        ),
        True,
    )


def _analyzed(correlation: Correlation) -> AnalyzedChange:
    change = correlation.change
    return AnalyzedChange(
        change_type=change.change_type,
        resource=change.resource,
        breaking=change.breaking,
        security_relevant=change.security_relevant,
        authentication_relevant=change.authentication_relevant,
        rationale=(change.evidence.excerpt or change.change_type.value)[:1000],
    )


def _items(nodes: list) -> list[AffectedItem]:  # type: ignore[type-arg]
    """Turn graph nodes into affected items, carrying their recorded evidence."""
    items = []
    for node in nodes:
        evidence = None
        if node.evidence:
            evidence = Evidence.model_validate(node.evidence)
        items.append(AffectedItem(key=node.key, label=node.label, evidence=evidence))
    return items


def evidence_confidence(assessment: ImpactAssessment) -> set[Confidence]:
    """Confidence levels present in an assessment's evidence.

    Exists so a test can assert that affected items carry CONFIRMED graph
    evidence rather than anything the model produced.
    """
    levels: set[Confidence] = set()
    for group in (
        assessment.affected_files,
        assessment.affected_symbols,
        assessment.affected_workflows,
        assessment.affected_tests,
    ):
        for item in group:
            if item.evidence is not None:
                levels.add(item.evidence.confidence)
    return levels
