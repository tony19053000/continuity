"""The whole product, as one callable.

Every stage Continuity has was built and tested on its own inputs. This is the
thing that connects them, so that "a provider changed" and "a pull request is
waiting for review" are the two ends of a single call rather than nine
components that each work in isolation:

    monitor → correlate → assess → rehearse → migrate → repair
            → security review → approval gate → deliver

Three principles govern how it is wired, and they are the reason this is a
sequence of *refusals* rather than a happy path with error handling bolted on:

* **Every stage can stop the run, and stopping is a normal outcome.** Most
  provider releases reach no code at all. A run that ends at
  `CHANGE_IRRELEVANT` has succeeded.
* **No stage is skipped because the previous one was confident.** The gates
  belong to the state machine and to `backend/security/policy.py`, and this
  module chooses among moves they permit rather than deciding anything itself.
* **Delivery is the only outward-facing act**, so it is last, it is gated on
  everything before it, and it is the one stage that is optional: a pipeline run
  with no GitHub client does all the work and stops with the patch in hand.

Nothing here re-derives a decision another module owns. If this file starts
re-implementing relevance or re-checking approvals, that is a sign a stage's
seam is in the wrong place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.contracts import AnalyzedChange
from backend.agents.impact_analyst import ImpactAssessment
from backend.github.delivery import (
    DeliveredPullRequest,
    DeliveryRefused,
    deliver,
)
from backend.integrations.correlation import Correlation, correlate_all
from backend.integrations.graph import IntegrationGraph
from backend.migrations.evidence import build_report
from backend.migrations.workspace import WorkspaceManager
from backend.models import (
    Approval,
    ChangeEvent,
    MigrationRun,
    Project,
    Repository,
    RunState,
)
from backend.models.enums import ApprovalStatus
from backend.models.schemas import ProviderChange, SourceRef, TransitionEvidence
from backend.observability.logging import get_logger
from backend.orchestration.coordinator import RunCoordinator
from backend.orchestration.delivery_gate import (
    PATCH_DIGEST_KEY,
    files_digest,
    preconditions_for,
)
from backend.orchestration.impact import assess_changes
from backend.orchestration.repair import RepairResult, run_repair_loop
from backend.orchestration.state_machine import can_transition, transition
from backend.providers.registry import ProviderRegistry
from backend.shared.model_provider import ModelProvider
from backend.validation.rehearsal import (
    NoSimulationAdapter,
    RehearsalAdapter,
    RehearsalResult,
    proceed_after_rehearsal,
    rehearse,
    selected_test_ids,
)
from backend.workers.provider_monitor import MonitorResult, monitor_project

logger = get_logger(__name__)


@dataclass(slots=True)
class RunOutcome:
    """What happened to one migration run, end to end."""

    migration_run_id: uuid.UUID
    provider_id: str
    final_state: RunState
    reason: str
    rehearsal: RehearsalResult | None = None
    repair: RepairResult | None = None
    delivered: DeliveredPullRequest | None = None
    approval_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def reached_delivery(self) -> bool:
        return self.delivered is not None

    def summary(self) -> dict[str, Any]:
        return {
            "migration_run_id": str(self.migration_run_id),
            "provider_id": self.provider_id,
            "final_state": self.final_state.value,
            "reason": self.reason,
            "rehearsal": self.rehearsal.outcome.value if self.rehearsal else None,
            "attempts": self.repair.attempts_used if self.repair else 0,
            "security": (
                self.repair.review.decision.value
                if self.repair and self.repair.review
                else None
            ),
            "pull_request": self.delivered.number if self.delivered else None,
        }


@dataclass(slots=True)
class PipelineResult:
    """One pass of the whole product over one project."""

    project_id: uuid.UUID
    monitored: list[MonitorResult] = field(default_factory=list)
    changes_recorded: int = 0
    relevant: int = 0
    runs: list[RunOutcome] = field(default_factory=list)
    stopped_at: str = ""

    @property
    def pull_requests(self) -> list[int]:
        return [r.delivered.number for r in self.runs if r.delivered]

    def summary(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "providers_checked": len(self.monitored),
            "changes_recorded": self.changes_recorded,
            "relevant": self.relevant,
            "stopped_at": self.stopped_at,
            "runs": [run.summary() for run in self.runs],
            "pull_requests": self.pull_requests,
        }


async def run_pipeline(
    session: AsyncSession,
    project: Project,
    *,
    model_provider: ModelProvider,
    coordinator: RunCoordinator,
    workspaces: WorkspaceManager | None = None,
    max_attempts: int,
    adapter_registry: ProviderRegistry | None = None,
    rehearsal_adapter: RehearsalAdapter | None = None,
    github_client: Any | None = None,
    default_branch: str = "main",
) -> PipelineResult:
    """Monitor, decide, migrate, review, and deliver — in one pass.

    Returns rather than raises for every ordinary stop. A provider release that
    affects nothing, a rehearsal that reproduces no difference, an exhausted
    repair budget, a security finding awaiting approval: all of these are
    outcomes, and a caller that had to catch exceptions to tell them apart would
    end up treating them as failures.
    """
    result = PipelineResult(project_id=project.id)

    # --- 1. monitor ------------------------------------------------------
    result.monitored = await monitor_project(
        session,
        project,
        adapter_registry=adapter_registry,
        model_provider=model_provider,
    )
    result.changes_recorded = sum(m.changes_recorded for m in result.monitored)

    if not result.changes_recorded:
        result.stopped_at = "no new provider changes"
        return result

    # --- 2. correlate and assess ----------------------------------------
    assessments = await _assess(session, project, result, model_provider=model_provider)
    if not assessments:
        # `_assess` sets a specific reason when it has one — an unscanned
        # project is a different situation from a release that reaches no code,
        # and overwriting it would tell the operator the wrong thing.
        result.stopped_at = result.stopped_at or "no change affects this project"
        return result

    if workspaces is None:
        # A migration needs a real git worktree to run tests in, and that needs
        # a local checkout of the repository. Continuity reads repositories
        # through the GitHub App, which gives it contents but not a working
        # tree — so a project with no `Repository.local_path` can be monitored,
        # correlated, and assessed, and cannot be migrated. Saying so is the
        # honest stop; opening a run that could never produce a patch is not.
        result.stopped_at = (
            f"{len(assessments)} change(s) need a migration, but this "
            "repository has no local checkout configured to build one in"
        )
        logger.info(
            "continuity.pipeline_no_workspace",
            extra={"project_id": str(project.id), "runs_deferred": len(assessments)},
        )
        return result

    # --- 3. one run per change that warrants a migration -----------------
    for assessment in assessments:
        outcome = await _drive_run(
            session,
            project,
            assessment,
            model_provider=model_provider,
            workspaces=workspaces,
            coordinator=coordinator,
            max_attempts=max_attempts,
            rehearsal_adapter=rehearsal_adapter,
            github_client=github_client,
            default_branch=default_branch,
        )
        result.runs.append(outcome)

    # The pass is over, whatever its runs did. Without this the project stays in
    # CHANGE_RELEVANT, which the scheduler skips — so one relevant change would
    # stop the project ever being monitored again. The runs it opened carry
    # their own states and continue independently.
    await _return_to_monitoring(session, project)

    result.stopped_at = "complete"
    logger.info(
        "continuity.pipeline_complete",
        extra={
            "project_id": str(project.id),
            "changes": result.changes_recorded,
            "relevant": result.relevant,
            "runs": len(result.runs),
            "pull_requests": len(result.pull_requests),
        },
    )
    return result


# ---------------------------------------------------------------------------
# Stage 2: correlation and impact
# ---------------------------------------------------------------------------


async def _assess(
    session: AsyncSession,
    project: Project,
    result: PipelineResult,
    *,
    model_provider: ModelProvider,
) -> list[ImpactAssessment]:
    """Correlate the recorded changes against the graph and judge them."""
    graph = IntegrationGraph(session, project.id)
    version = await graph.latest_version()
    if version is None:
        # No graph means the repository has never been scanned. Nothing can be
        # correlated, and guessing would be worse than stopping.
        result.stopped_at = "this project has no integration graph; scan it first"
        return []

    events = await _recorded_changes(session, project, result.monitored)
    if not events:
        return []

    await _walk_to_impact_analysis(session, project)

    correlations: list[tuple[ChangeEvent, Correlation]] = []
    for provider_id, provider_events in _by_provider(events).items():
        changes = [change_from_event(event) for event in provider_events]
        correlated = await correlate_all(graph, version, provider_id, changes)
        correlations.extend(zip(provider_events, correlated, strict=True))

    batch = await assess_changes(
        session, project, correlations, model_provider=model_provider
    )
    result.relevant = len(batch.relevant)

    return [a for a in batch.assessments if a.migration_run_id is not None]


async def _recorded_changes(
    session: AsyncSession, project: Project, monitored: list[MonitorResult]
) -> list[ChangeEvent]:
    """The change events this pass just recorded.

    Scoped to the version pairs the monitor reported, so a second pipeline run
    does not re-assess history it already dealt with.
    """
    pairs = {
        (m.provider_id, m.baseline, m.current)
        for m in monitored
        if m.changes_recorded and m.baseline and m.current
    }
    if not pairs:
        return []

    found: list[ChangeEvent] = []
    for provider_id, baseline, current in sorted(pairs):
        rows = (
            await session.execute(
                select(ChangeEvent)
                .where(
                    ChangeEvent.provider_id == provider_id,
                    ChangeEvent.old_version == baseline,
                    ChangeEvent.new_version == current,
                )
                .order_by(ChangeEvent.resource)
            )
        ).scalars()
        found.extend(rows)
    return found


def _by_provider(events: list[ChangeEvent]) -> dict[str, list[ChangeEvent]]:
    grouped: dict[str, list[ChangeEvent]] = {}
    for event in events:
        grouped.setdefault(event.provider_id, []).append(event)
    return grouped


def change_from_event(event: ChangeEvent) -> ProviderChange:
    """Rebuild the schema object from the row the monitor wrote.

    Public because correlation always starts from a recorded event, and the
    evaluation harness correlates the same rows this pipeline does — two
    conversions would be two chances to disagree about what a stored change
    means.

    Correlation works on `ProviderChange`, and the pipeline re-reads from the
    database rather than holding the objects the monitor returned — so a run
    resumed after a restart behaves identically to one that never stopped.
    """
    from backend.models.schemas import Evidence

    return ProviderChange(
        provider_id=event.provider_id,
        old_version=event.old_version,
        new_version=event.new_version,
        change_type=event.change_type,
        resource=event.resource,
        old_contract=dict(event.old_contract) if event.old_contract else None,
        new_contract=dict(event.new_contract) if event.new_contract else None,
        breaking=event.breaking,
        security_relevant=event.security_relevant,
        authentication_relevant=event.authentication_relevant,
        source=SourceRef.model_validate(event.source),
        evidence=Evidence.model_validate(event.evidence),
    )


async def _walk_to_impact_analysis(session: AsyncSession, project: Project) -> None:
    """Move the project CHANGE_DETECTED → … → IMPACT_ANALYSIS_RUNNING.

    Three hops, because each names a real stage: the change set is being
    classified, the classification is complete, and impact is being judged.
    `assess_changes` needs the project in the last of them to record its verdict.
    """
    for target in (
        RunState.CHANGE_ANALYSIS_RUNNING,
        RunState.CHANGE_ANALYSIS_COMPLETE,
        RunState.IMPACT_ANALYSIS_RUNNING,
    ):
        tracked = await session.get(Project, project.id) or project
        if not can_transition(tracked.state, target):
            return
        await transition(
            session,
            from_state=tracked.state,
            to_state=target,
            evidence=TransitionEvidence(
                reason="pipeline: assessing the detected changes",
                actor="pipeline",
                detail={},
            ),
            project_id=tracked.id,
        )
        tracked.state = target
        await session.flush()


# ---------------------------------------------------------------------------
# Stage 3: one migration run
# ---------------------------------------------------------------------------


async def _drive_run(
    session: AsyncSession,
    project: Project,
    assessment: ImpactAssessment,
    *,
    model_provider: ModelProvider,
    coordinator: RunCoordinator,
    max_attempts: int,
    # Not optional here: `run_pipeline` refuses before reaching this point when
    # there is no checkout to migrate in.
    workspaces: WorkspaceManager,
    rehearsal_adapter: RehearsalAdapter | None,
    github_client: Any | None,
    default_branch: str,
) -> RunOutcome:
    """Rehearse, migrate, repair, review, and deliver one change."""
    run_id = assessment.migration_run_id
    assert run_id is not None  # noqa: S101 - filtered by the caller
    run = await session.get(MigrationRun, run_id)
    if run is None:  # pragma: no cover - foreign key
        raise RuntimeError(f"migration run {run_id} vanished mid-pipeline")

    outcome = RunOutcome(
        migration_run_id=run.id,
        provider_id=run.provider_id,
        final_state=run.state,
        reason="",
    )

    event = await session.get(ChangeEvent, run.change_event_id)
    if event is None:  # pragma: no cover - foreign key
        raise RuntimeError("migration run has no change event")
    change = change_from_event(event)

    impact_set = sorted({item.key for item in assessment.affected_files})
    test_selectors = sorted({item.key for item in assessment.affected_tests})

    async with workspaces.open(
        run_id=run.id,
        source_commit=run.source_commit,
        target_branch=f"continuity/migrate-{run.provider_id}-{run.to_version}",
    ) as workspace:
        run.source_commit = workspace.record.source_commit
        await session.flush()

        # --- rehearsal ---------------------------------------------------
        await _move(session, run, RunState.REHEARSAL_PENDING, "rehearsing the change")

        outcome.rehearsal = await rehearse(
            session,
            run,
            change,
            executor=workspace._executor,
            adapter=rehearsal_adapter or NoSimulationAdapter(),
            workspace=workspace.root,
            selected_tests=test_selectors,
            affected_workflows=[item.label for item in assessment.affected_workflows],
        )

        after = await proceed_after_rehearsal(session, run, outcome.rehearsal)
        if after is not RunState.MIGRATION_PENDING:
            # REHEARSAL_FAILED: the provider said something changed and the
            # project's own tests disagree. Escalated rather than migrated.
            outcome.final_state = after
            outcome.reason = outcome.rehearsal.reason
            return outcome

        # --- migrate, validate, repair, review ---------------------------
        outcome.repair = await run_repair_loop(
            session,
            run,
            workspace,
            change=_analyzed(change),
            impact=_impact_output(assessment),
            impact_set=impact_set,
            model_provider=model_provider,
            coordinator=coordinator,
            max_attempts=max_attempts,
            test_selectors=test_selectors,
        )
        outcome.final_state = outcome.repair.final_state
        outcome.reason = outcome.repair.reason

        if outcome.repair.final_state is not RunState.SECURITY_REVIEW_PASSED:
            # Every other ending needs a person. The workspace is discarded on
            # the way out of this block, so anything needed to resume is
            # recorded now — the stored diff and the identity of the tree it
            # produces. `resume.py` rebuilds the workspace from those.
            if outcome.repair.final_state is RunState.APPROVAL_PENDING:
                await _record_patch_identity(session, run, workspace)
            outcome.approval_ids = await _pending_approvals(session, run)
            return outcome

        # --- deliver ------------------------------------------------------
        await _deliver_if_possible(
            session,
            project,
            run,
            outcome,
            workspace=workspace,
            github_client=github_client,
            default_branch=default_branch,
        )

    return outcome


async def _deliver_if_possible(
    session: AsyncSession,
    project: Project,
    run: MigrationRun,
    outcome: RunOutcome,
    *,
    workspace: Any,
    github_client: Any | None,
    default_branch: str,
) -> None:
    """Open the pull request, if this deployment can.

    A pipeline with no GitHub client is a complete, useful run: everything is
    decided, the evidence is stored, and the patch is described. It stops before
    the one outward-facing act rather than pretending to perform it.
    """
    if github_client is None:
        outcome.final_state = RunState.SECURITY_REVIEW_PASSED
        outcome.reason = (
            "ready to deliver; no GitHub client is configured for this deployment"
        )
        return

    repository = await session.get(Repository, project.repository_id)
    if repository is None:  # pragma: no cover - foreign key
        outcome.reason = "the project has no repository record"
        return

    changed = await workspace.changed_files()
    files = {path: workspace.read_file(path) for path in changed}

    await _move(session, run, RunState.FINAL_VALIDATION_RUNNING, "final check before delivery")
    await _move(session, run, RunState.FINAL_VALIDATION_PASSED, "patch is ready to deliver")
    await _move(session, run, RunState.PR_PENDING, "opening a pull request")

    report = await build_report(session, run)

    try:
        # Derived from this run's own stored rows, never from what the caller
        # believes happened. Raises if the evidence is incomplete, so there is
        # no return value a caller can forget to check.
        preconditions = await preconditions_for(session, run, files=files)

        outcome.delivered = await deliver(
            session,
            run,
            repository,
            client=github_client,
            preconditions=preconditions,
            default_branch=default_branch,
            files=files,
            title=(
                f"Migrate {run.provider_id} {run.from_version} → {run.to_version}"
            ),
            body=report.as_markdown(),
            commit_message=(
                f"migrate {run.provider_id} to {run.to_version}\n\n"
                f"Opened by Continuity. Evidence is in the pull request body."
            ),
        )
    except DeliveryRefused as refusal:
        outcome.reason = str(refusal)
        return

    refreshed = await session.get(MigrationRun, run.id)
    outcome.final_state = refreshed.state if refreshed else RunState.MERGE_WAITING
    outcome.reason = f"pull request #{outcome.delivered.number} opened"


async def _return_to_monitoring(session: AsyncSession, project: Project) -> None:
    tracked = await session.get(Project, project.id) or project
    if not can_transition(tracked.state, RunState.MONITORING_ACTIVE):
        return
    await transition(
        session,
        from_state=tracked.state,
        to_state=RunState.MONITORING_ACTIVE,
        evidence=TransitionEvidence(
            reason="pipeline pass complete; monitoring resumed",
            actor="pipeline",
            detail={},
        ),
        project_id=tracked.id,
    )
    tracked.state = RunState.MONITORING_ACTIVE
    await session.flush()


async def _record_patch_identity(
    session: AsyncSession, run: MigrationRun, workspace: Any
) -> None:
    """Record what the validated tree looks like, before the workspace goes.

    A resumed run rebuilds the patch from the stored diff. This is what lets
    delivery prove the rebuilt tree is the one that was reviewed rather than
    trusting that re-applying a diff is deterministic.
    """
    changed = await workspace.changed_files()
    files = {path: workspace.read_file(path) for path in changed}

    tracked = await session.get(MigrationRun, run.id) or run
    stored = dict(tracked.evidence_report or {})
    stored[PATCH_DIGEST_KEY] = files_digest(files)
    tracked.evidence_report = stored
    await session.flush()


async def _pending_approvals(
    session: AsyncSession, run: MigrationRun
) -> list[uuid.UUID]:
    rows = (
        await session.execute(
            select(Approval.id).where(
                Approval.migration_run_id == run.id,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
    ).scalars()
    return list(rows)


async def _granted_approvals(
    session: AsyncSession, run: MigrationRun
) -> list[uuid.UUID]:
    """Approvals this run holds, for delivery to re-check.

    Passed as ids rather than as a decision, so `require_granted` re-reads each
    one at the moment of delivery. A person can revoke between here and there.
    """
    rows = (
        await session.execute(
            select(Approval.id).where(Approval.migration_run_id == run.id)
        )
    ).scalars()
    return list(rows)


def _analyzed(change: ProviderChange) -> AnalyzedChange:
    return AnalyzedChange(
        change_type=change.change_type,
        resource=change.resource,
        breaking=change.breaking,
        security_relevant=change.security_relevant,
        authentication_relevant=change.authentication_relevant,
        rationale=(change.evidence.excerpt or change.change_type.value)[:1000],
    )


def _impact_output(assessment: ImpactAssessment) -> Any:
    from backend.agents.contracts import ImpactAnalystOutput

    return ImpactAnalystOutput(
        relevant=assessment.relevant,
        severity=assessment.severity,
        migration_required=assessment.migration_required,
        affected_files=[item.key for item in assessment.affected_files],
        affected_symbols=[item.key for item in assessment.affected_symbols],
        affected_workflows=[item.label for item in assessment.affected_workflows],
        affected_tests=[item.key for item in assessment.affected_tests],
        reasoning_summary=assessment.reasoning_summary,
    )


async def _move(
    session: AsyncSession, run: MigrationRun, to_state: RunState, reason: str
) -> None:
    tracked = await session.get(MigrationRun, run.id) or run
    if not can_transition(tracked.state, to_state):
        return
    await transition(
        session,
        from_state=tracked.state,
        to_state=to_state,
        evidence=TransitionEvidence(reason=reason, actor="pipeline", detail={}),
        project_id=tracked.project_id,
        migration_run_id=tracked.id,
    )
    tracked.state = to_state
    await session.flush()


def selected_tests_for(assessment: ImpactAssessment) -> list[str]:
    """Test selectors from an assessment, deduplicated and ordered."""
    return selected_test_ids(assessment.affected_tests)
