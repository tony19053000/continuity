"""Getting a repository from "connected" to "monitored".

The front half of the product loop, as one callable:

    create/select project → scan → deterministic extraction
        → integration mapping → Integration Intelligence Graph
        → baseline → MONITORING_ACTIVE

Each of these already existed and worked. None of them had a production caller,
which meant a real project could never reach the state the rest of the pipeline
requires — `run_pipeline` stops immediately without a graph, and nothing built
one. This is the module that closes that gap.

Two rules it follows:

* **No stage is skipped.** Every state hop is a recorded transition, so the
  timeline shows scanning, mapping, and baselining as separate events rather
  than one leap to "ready".
* **A failure leaves the project visibly failed**, not half-onboarded. A
  partially indexed project that still read as MONITORING_ACTIVE would be
  monitored against a baseline that describes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.baseline import BaselineResult, establish_baseline
from backend.integrations.mapper import MappingResult, map_integrations
from backend.models import Project, RunState
from backend.models.schemas import TransitionEvidence
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.repository.source import RepositorySource
from backend.shared.model_provider import ModelProvider
from backend.workers.repository_scan import ScanResult, run_repository_scan

logger = get_logger(__name__)

#: The states a project walks before a scan can start. Each names a real step,
#: and `ALLOWED_TRANSITIONS` is what enforces the order.
_TO_SCAN_PENDING = (
    RunState.GITHUB_CONNECTED,
    RunState.REPOSITORY_SELECTED,
    RunState.INITIAL_SCAN_PENDING,
)


@dataclass(slots=True)
class OnboardingResult:
    """What onboarding produced, and whether the project is now monitorable."""

    project_id: uuid.UUID
    scan: ScanResult | None = None
    mapping: MappingResult | None = None
    baseline: BaselineResult | None = None
    final_state: RunState = RunState.PROJECT_CREATED
    reason: str = ""

    @property
    def monitorable(self) -> bool:
        """Whether the scheduler will now pick this project up."""
        return self.final_state is RunState.MONITORING_ACTIVE

    def summary(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "final_state": self.final_state.value,
            "monitorable": self.monitorable,
            "reason": self.reason,
            "files_indexed": (self.scan.summary.get("files_indexed") if self.scan else 0),
            "graph_version": self.mapping.graph_version if self.mapping else None,
            "confirmed_nodes": self.mapping.confirmed_nodes if self.mapping else 0,
            "inferred_workflows": self.mapping.inferred_workflows if self.mapping else 0,
            "providers": len(self.baseline.entries) if self.baseline else 0,
            "mapping_degraded": (
                self.mapping.agent_skipped_reason if self.mapping else None
            ),
        }


async def onboard_project(
    session: AsyncSession,
    project: Project,
    source: RepositorySource,
    *,
    model_provider: ModelProvider | None = None,
) -> OnboardingResult:
    """Scan, map, and baseline a project until the scheduler can monitor it.

    `model_provider` is optional. Without one the mapper records confirmed
    extraction and skips workflow inference, which degrades the graph rather
    than failing onboarding — the deterministic half is what the rest of the
    pipeline correlates against.
    """
    result = OnboardingResult(project_id=project.id, final_state=project.state)

    try:
        await _walk(session, project, _TO_SCAN_PENDING, "preparing to scan")

        result.scan = await run_repository_scan(session, project, source)

        result.mapping = await map_integrations(
            session,
            project_id=project.id,
            index=result.scan.index,
            source=source,
            model_provider=model_provider,
        )

        result.baseline = await establish_baseline(
            session, project, graph_version=result.mapping.graph_version
        )
    except Exception as exc:
        # `run_repository_scan` already moves the project to RUN_FAILED on its
        # own failure. Mapping and baselining do not, and a project left in
        # INTEGRATION_MAPPING_RUNNING would be invisible to both the scheduler
        # and the operator.
        await _fail(session, project, f"onboarding failed: {type(exc).__name__}")
        result.final_state = project.state
        result.reason = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "continuity.onboarding_failed",
            extra={"project_id": str(project.id), "error": type(exc).__name__},
        )
        raise

    result.final_state = project.state
    result.reason = (
        "monitoring active"
        if result.monitorable
        else f"onboarding stopped at {project.state.value}"
    )

    logger.info("continuity.onboarding_complete", extra=result.summary())
    return result


async def _walk(
    session: AsyncSession,
    project: Project,
    states: tuple[RunState, ...],
    reason: str,
) -> None:
    """Move through a documented sequence, skipping hops already taken.

    Re-onboarding an existing project is a legitimate action — a repository
    changes — so a state already reached is passed over rather than treated as
    an error.
    """
    for target in states:
        if project.state is target:
            continue
        if not can_transition(project.state, target):
            # The project is somewhere the sequence does not start from. Most
            # often it is already monitoring, which `_from_monitoring` handles.
            continue
        await transition(
            session,
            from_state=project.state,
            to_state=target,
            evidence=TransitionEvidence(reason=reason, actor="onboarding", detail={}),
            project_id=project.id,
        )
        project.state = target
        await session.flush()


async def _fail(session: AsyncSession, project: Project, reason: str) -> None:
    tracked = await session.get(Project, project.id) or project
    if tracked.state is RunState.RUN_FAILED:
        return
    if not can_transition(tracked.state, RunState.RUN_FAILED):
        return
    await transition(
        session,
        from_state=tracked.state,
        to_state=RunState.RUN_FAILED,
        evidence=TransitionEvidence(reason=reason, actor="onboarding", detail={}),
        project_id=tracked.id,
    )
    tracked.state = RunState.RUN_FAILED
    await session.flush()


async def rescan_project(
    session: AsyncSession,
    project: Project,
    source: RepositorySource,
    *,
    model_provider: ModelProvider | None = None,
) -> OnboardingResult:
    """Re-run onboarding on a project that is already monitoring.

    A repository changes, and the graph has to keep up. `MONITORING_ACTIVE →
    INITIAL_SCAN_PENDING` is an edge the state machine already allows, which is
    what makes this a normal operation rather than a special case.
    """
    if project.state is RunState.MONITORING_ACTIVE and can_transition(
        project.state, RunState.INITIAL_SCAN_PENDING
    ):
        await transition(
            session,
            from_state=project.state,
            to_state=RunState.INITIAL_SCAN_PENDING,
            evidence=TransitionEvidence(
                reason="rescan requested", actor="onboarding", detail={}
            ),
            project_id=project.id,
        )
        project.state = RunState.INITIAL_SCAN_PENDING
        await session.flush()

    return await onboard_project(
        session, project, source, model_provider=model_provider
    )
