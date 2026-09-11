"""Running one labelled case through production code, and reading back what it did.

Nothing here reimplements a stage. Onboarding is `onboard_project`, detection is
`monitor_project`, correlation is `correlate_all`, and the migration half is
`run_pipeline` — the same functions the scheduler calls. The harness's only job
is to set a case up, drive those, and collect what was *recorded*.

**Two planes, and the difference is stated in every report.**

*Deterministic* always runs and needs no model: detection, breaking-change
classification, localisation, and Integration Health are all decided by code —
the spec differ, the correlator, the health formula — so they can be scored
against labels on any machine, including CI.

*Execution* needs a model, because the Migration Engineer is a model. With none
configured the harness does not script one and pretend: the execution metrics
report as unavailable with that reason. A scripted engineer would measure the
script.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.health_score import IntegrationHealth, integration_health
from backend.evaluation.fixtures import FixtureProviderAdapter, materialise
from backend.evaluation.labels import LabelledCase
from backend.integrations.correlation import Correlation, correlate_all
from backend.integrations.graph import IntegrationGraph
from backend.migrations.workspace import WorkspaceManager
from backend.models import (
    ChangeEvent,
    MigrationAttempt,
    MigrationRun,
    Project,
    PullRequest,
    Repository,
    SecurityFinding,
    ToolInvocation,
    User,
)
from backend.models.enums import ApprovalStatus, AttemptOutcome, PolicyDecision, RunState
from backend.observability.execution_audit import NullExecutionAudit
from backend.observability.logging import get_logger
from backend.observability.usage import UsageLog, collecting
from backend.orchestration.coordinator import RunCoordinator
from backend.orchestration.onboarding import onboard_project
from backend.orchestration.pipeline import change_from_event, run_pipeline
from backend.providers.registry import ProviderRegistry
from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.shared.model_provider import ModelProvider
from backend.workers.provider_monitor import monitor_project

logger = get_logger(__name__)


@dataclass(slots=True)
class CaseObservation:
    """Everything the harness saw for one case. No judgments, only records."""

    case: LabelledCase
    onboarded: bool = False
    graph_version: int | None = None

    #: Detection
    detection_ms: int = 0
    detected_resources: list[str] = field(default_factory=list)
    detected_breaking: dict[str, bool] = field(default_factory=dict)
    #: The rows this case's monitor pass wrote. `change_events` is global —
    #: providers are not scoped to a project — so a case that queried by
    #: provider and version would see every other case's changes, and one that
    #: should have detected nothing would look like it detected four things.
    detected_event_ids: list[uuid.UUID] = field(default_factory=list)

    #: Localisation, from deterministic correlation
    correlated_files: list[str] = field(default_factory=list)
    correlated_workflows: list[str] = field(default_factory=list)
    correlation_empty: bool = True

    #: Execution, from stored rows. `None` means the plane did not run.
    runs_started: int | None = None
    attempts: list[AttemptOutcome] = field(default_factory=list)
    reached_delivery: bool = False
    pull_requests: int = 0
    final_state: RunState | None = None
    tests_ran: bool = False
    tests_passed: bool = False

    #: Safety
    blocking_findings: int = 0
    approvals_requested: int = 0
    approvals_unresolved: int = 0
    refusals: int = 0

    #: Cost
    elapsed_ms: int = 0
    tool_invocations: int = 0
    usage: UsageLog | None = None

    health: IntegrationHealth | None = None
    error: str = ""

    @property
    def executed(self) -> bool:
        """Whether the execution plane ran for this case."""
        return self.runs_started is not None

    def summary(self) -> dict[str, Any]:
        return {
            "case_id": self.case.case_id,
            "onboarded": self.onboarded,
            "graph_version": self.graph_version,
            "detected": list(self.detected_resources),
            "correlated_files": list(self.correlated_files),
            "executed": self.executed,
            "final_state": self.final_state.value if self.final_state else None,
            "reached_delivery": self.reached_delivery,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


async def run_case(
    session: AsyncSession,
    case: LabelledCase,
    *,
    workspace_root: Path,
    checkout_root: Path,
    model_provider: ModelProvider | None = None,
    max_attempts: int = 3,
) -> CaseObservation:
    """Set one case up, drive production code over it, and read the records."""
    observation = CaseObservation(case=case)
    started = time.perf_counter()

    with collecting() as usage:
        try:
            checkout = await materialise(case, checkout_root)
            project = await _project_for(session, case, checkout)
            adapter = FixtureProviderAdapter(case)

            await _onboard(session, project, checkout, observation, model_provider)
            if observation.onboarded:
                await _detect(session, project, adapter, observation, model_provider)
                await _localise(session, project, case, observation)
                if model_provider is not None:
                    await _execute(
                        session,
                        project,
                        adapter,
                        observation,
                        checkout=checkout,
                        workspace_root=workspace_root,
                        model_provider=model_provider,
                        max_attempts=max_attempts,
                    )
                await _collect_records(session, project, observation)
                observation.health = await integration_health(session, project)
        except Exception as exc:
            # Recorded, not raised. A harness that stops at the first failing
            # case reports nothing about the other twenty, and "the evaluation
            # crashed" is a much less useful answer than "this case failed".
            observation.error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "continuity.evaluation_case_failed",
                extra={"case_id": case.case_id, "error": type(exc).__name__},
            )

    observation.usage = usage
    observation.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return observation


async def _project_for(
    session: AsyncSession, case: LabelledCase, checkout: Path
) -> Project:
    user = User(
        email=f"evaluation+{case.case_id}@continuity.invalid",
        google_subject=uuid.uuid4().hex,
    )
    session.add(user)
    await session.flush()

    repository = Repository(
        owner="evaluation",
        name=case.case_id,
        default_branch="main",
        local_path=str(checkout),
    )
    session.add(repository)
    await session.flush()

    project = Project(
        user_id=user.id,
        repository_id=repository.id,
        name=case.case_id,
        state=RunState.PROJECT_CREATED,
    )
    session.add(project)
    await session.flush()
    return project


async def _onboard(
    session: AsyncSession,
    project: Project,
    checkout: Path,
    observation: CaseObservation,
    model_provider: ModelProvider | None,
) -> None:
    result = await onboard_project(
        session,
        project,
        LocalRepositoryAdapter(checkout),
        model_provider=model_provider,
    )
    observation.onboarded = result.final_state is RunState.MONITORING_ACTIVE
    observation.graph_version = (
        result.mapping.graph_version if result.mapping is not None else None
    )


async def _detect(
    session: AsyncSession,
    project: Project,
    adapter: FixtureProviderAdapter,
    observation: CaseObservation,
    model_provider: ModelProvider | None,
) -> None:
    """Advance the provider, then measure how long noticing takes.

    Latency here is *time to notice once Continuity looks*, not time since the
    provider published. The second one is dominated by the poll interval, which
    is configuration rather than performance, and reporting it as a product
    measurement would say more about a settings file than about the code.
    """
    registry = ProviderRegistry()
    registry.register(adapter)
    adapter.advance()

    before = await _event_ids(session)

    started = time.perf_counter()
    await monitor_project(
        session,
        project,
        adapter_registry=registry,
        model_provider=model_provider,
    )
    observation.detection_ms = int((time.perf_counter() - started) * 1000)

    new_ids = await _event_ids(session) - before
    observation.detected_event_ids = sorted(new_ids)
    events = (
        list(
            (
                await session.execute(
                    select(ChangeEvent).where(ChangeEvent.id.in_(new_ids))
                )
            ).scalars()
        )
        if new_ids
        else []
    )
    observation.detected_resources = sorted(event.resource for event in events)
    observation.detected_breaking = {
        event.resource: bool(event.breaking) for event in events
    }


async def _event_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Every change event that exists right now."""
    return set((await session.execute(select(ChangeEvent.id))).scalars())


async def _localise(
    session: AsyncSession,
    project: Project,
    case: LabelledCase,
    observation: CaseObservation,
) -> None:
    """Which files the change reaches, decided by code rather than by a model.

    Correlated from the `change_events` rows the monitor wrote, through the same
    conversion the pipeline uses. Re-running the differ here would measure a
    second derivation of the changes rather than the ones Continuity recorded.
    """
    if observation.graph_version is None or not observation.detected_event_ids:
        return

    events = list(
        (
            await session.execute(
                select(ChangeEvent).where(
                    ChangeEvent.id.in_(observation.detected_event_ids)
                )
            )
        ).scalars()
    )
    if not events:
        return

    graph = IntegrationGraph(session, project.id)
    correlations: list[Correlation] = await correlate_all(
        graph,
        observation.graph_version,
        case.provider_id,
        [change_from_event(event) for event in events],
    )

    files: set[str] = set()
    workflows: set[str] = set()
    for correlation in correlations:
        if correlation.is_empty:
            continue
        observation.correlation_empty = False
        if correlation.blast is not None:
            files.update(node.key for node in correlation.blast.files)
            workflows.update(node.label for node in correlation.blast.workflows)

    observation.correlated_files = sorted(files)
    observation.correlated_workflows = sorted(workflows)


async def _execute(
    session: AsyncSession,
    project: Project,
    adapter: FixtureProviderAdapter,
    observation: CaseObservation,
    *,
    checkout: Path,
    workspace_root: Path,
    model_provider: ModelProvider,
    max_attempts: int,
) -> None:
    """The migration half, run exactly as the scheduler runs it."""
    registry = ProviderRegistry()
    registry.register(adapter)

    result = await run_pipeline(
        session,
        project,
        model_provider=model_provider,
        workspaces=WorkspaceManager(
            checkout, workspace_root=workspace_root, audit=NullExecutionAudit()
        ),
        coordinator=RunCoordinator(max_repair_attempts=max_attempts),
        max_attempts=max_attempts,
        adapter_registry=registry,
    )

    observation.runs_started = len(result.runs)
    for outcome in result.runs:
        observation.final_state = outcome.final_state
        observation.reached_delivery = (
            observation.reached_delivery or outcome.reached_delivery
        )
    observation.pull_requests = len(result.pull_requests)


async def _collect_records(
    session: AsyncSession, project: Project, observation: CaseObservation
) -> None:
    """Read what the run recorded. Rows only — nothing inferred."""
    runs = list(
        (
            await session.execute(
                select(MigrationRun).where(MigrationRun.project_id == project.id)
            )
        ).scalars()
    )
    if not runs:
        return

    run_ids = [run.id for run in runs]

    attempts = list(
        (
            await session.execute(
                select(MigrationAttempt)
                .where(MigrationAttempt.migration_run_id.in_(run_ids))
                .order_by(MigrationAttempt.attempt_number)
            )
        ).scalars()
    )
    observation.attempts = [
        attempt.outcome for attempt in attempts if attempt.outcome is not None
    ]
    observation.tests_ran = any(
        attempt.tests_executed is not None for attempt in attempts
    )
    observation.tests_passed = any(
        attempt.outcome is AttemptOutcome.PASSED for attempt in attempts
    )

    findings = list(
        (
            await session.execute(
                select(SecurityFinding).where(
                    SecurityFinding.migration_run_id.in_(run_ids)
                )
            )
        ).scalars()
    )
    observation.blocking_findings = sum(
        1
        for finding in findings
        if finding.policy_decision is not PolicyDecision.ALLOW
    )
    observation.refusals = sum(
        1 for finding in findings if finding.policy_decision is PolicyDecision.DENY
    )

    from backend.models import Approval

    approvals = list(
        (
            await session.execute(
                select(Approval).where(Approval.migration_run_id.in_(run_ids))
            )
        ).scalars()
    )
    observation.approvals_requested = len(approvals)
    observation.approvals_unresolved = sum(
        1 for approval in approvals if approval.status is ApprovalStatus.PENDING
    )

    observation.pull_requests = max(
        observation.pull_requests,
        len(
            list(
                (
                    await session.execute(
                        select(PullRequest).where(
                            PullRequest.migration_run_id.in_(run_ids)
                        )
                    )
                ).scalars()
            )
        ),
    )

    # Tool invocations are linked to an `agent_run`, and nothing writes those
    # rows yet — so they cannot be attributed to a migration run. Counted for
    # the whole evaluation database instead, which is created fresh per run, and
    # reported as a total rather than as a per-case figure it is not.
    observation.tool_invocations = len(
        list((await session.execute(select(ToolInvocation))).scalars())
    )
