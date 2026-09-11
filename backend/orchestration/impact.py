"""C6-03 orchestration: what an impact assessment *does*.

Separated from `backend/agents/impact_analyst.py` on purpose. Only the
coordinator moves runs, and no module under `backend/agents/` may import the
state machine — so the agent judges, and this decides what follows from the
judgment: which changes open a migration run, and where the project goes next.

The selectivity that matters is enforced here. A migration run is created
**only** for a change that is both relevant and needs a migration, so counting
`migration_runs` rows is a direct measure of how much noise this stage absorbs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.impact_analyst import ImpactAssessment, assess_change
from backend.integrations.correlation import Correlation
from backend.models import ChangeEvent, MigrationRun, Project
from backend.models.enums import ActivityEventKind, RunState
from backend.models.schemas import Evidence, TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.shared.model_provider import ModelProvider

logger = get_logger(__name__)


@dataclass(slots=True)
class AssessmentBatch:
    """The result of assessing one provider's change set against one project."""

    assessments: list[ImpactAssessment] = field(default_factory=list)
    model_calls: int = 0
    runs_created: int = 0

    @property
    def relevant(self) -> list[ImpactAssessment]:
        return [a for a in self.assessments if a.relevant]

    @property
    def irrelevant(self) -> list[ImpactAssessment]:
        return [a for a in self.assessments if not a.relevant]


async def assess_changes(
    session: AsyncSession,
    project: Project,
    correlations: list[tuple[ChangeEvent, Correlation]],
    *,
    model_provider: ModelProvider | None = None,
    source_slices: dict[str, list[str]] | None = None,
) -> AssessmentBatch:
    """Assess a provider's change set and open runs for what warrants one.

    A migration run is created **only** for a change that is both relevant and
    needs a migration. Counting `migration_runs` rows is therefore a direct
    measure of how selective this stage is, which is what C6-03's acceptance
    asks for.
    """
    batch = AssessmentBatch()
    slices = source_slices or {}

    for change_event, correlation in correlations:
        assessment, used_model = await assess_change(
            correlation,
            change_event.id,
            project_id=project.id,
            model_provider=model_provider,
            source_slices=slices.get(correlation.change.resource),
        )
        batch.model_calls += int(used_model)

        if assessment.migration_required:
            run = MigrationRun(
                project_id=project.id,
                change_event_id=change_event.id,
                provider_id=correlation.change.provider_id,
                from_version=correlation.change.old_version,
                to_version=correlation.change.new_version,
                state=RunState.CHANGE_RELEVANT,
                evidence_report=assessment.summary(),
            )
            session.add(run)
            await session.flush()
            assessment.migration_run_id = run.id
            batch.runs_created += 1

            await events.emit(
                session,
                kind=ActivityEventKind.IMPACTED_WORKFLOW_DETECTED,
                actor="impact_analyst",
                summary=(
                    f"{correlation.change.change_type.value} on "
                    f"{correlation.change.resource} affects "
                    f"{len(assessment.affected_workflows)} workflow(s)"
                ),
                project_id=project.id,
                migration_run_id=run.id,
                evidence=_first_evidence(assessment),
            )

        batch.assessments.append(assessment)

    await _record_outcome(session, project, batch)

    logger.info(
        "continuity.impact_assessed",
        extra={
            "project_id": str(project.id),
            "changes": len(batch.assessments),
            "relevant": len(batch.relevant),
            "model_calls": batch.model_calls,
            "runs_created": batch.runs_created,
        },
    )
    return batch


def _first_evidence(assessment: ImpactAssessment) -> Evidence | None:
    for group in (
        assessment.affected_symbols,
        assessment.affected_files,
        assessment.affected_tests,
    ):
        for item in group:
            if item.evidence is not None:
                return item.evidence
    return None


async def _record_outcome(
    session: AsyncSession, project: Project, batch: AssessmentBatch
) -> None:
    """Move the project to reflect what the assessment found.

    `CHANGE_IRRELEVANT` when nothing reached this codebase — the common case,
    and the one that returns the project to monitoring without a review cycle.
    """
    tracked = await session.get(Project, project.id) or project
    target = (
        RunState.CHANGE_RELEVANT if batch.relevant else RunState.CHANGE_IRRELEVANT
    )

    if not can_transition(tracked.state, target):
        logger.info(
            "continuity.impact_outcome_not_applied",
            extra={
                "project_id": str(project.id),
                "from_state": tracked.state.value,
                "to_state": target.value,
            },
        )
        return

    await transition(
        session,
        from_state=tracked.state,
        to_state=target,
        evidence=TransitionEvidence(
            reason=(
                f"{len(batch.relevant)} of {len(batch.assessments)} change(s) "
                "affect this project"
            ),
            actor="impact_analyst",
            detail={
                "relevant": len(batch.relevant),
                "irrelevant": len(batch.irrelevant),
                "model_calls": batch.model_calls,
                "runs_created": batch.runs_created,
            },
        ),
        project_id=project.id,
    )
    tracked.state = target
    await session.flush()


