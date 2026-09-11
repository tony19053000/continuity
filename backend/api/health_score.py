"""The Integration Health score, exactly as `02_ARCHITECTURE.md` §18 defines it.

Two rules govern this module, and both come from the anchor documents:

* **Every input traces to a stored record.** The score is arithmetic over rows —
  unresolved change events, pending approvals, graph coverage. No part of it is
  a model's opinion about how healthy a project feels.
* **A score whose inputs are absent is not displayed at all.** A project that
  has never been scanned has no integration points, and rendering "100%" for it
  would be a lie about system state (`04_FRONTEND_SPEC.md` §3.6). `available`
  is False in that case, and the UI shows nothing rather than a placeholder.

The formula is published beside the score so a reader can check the arithmetic,
which is the point of publishing it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Approval,
    ChangeEvent,
    GraphEdge,
    GraphNode,
    MigrationRun,
    Project,
)
from backend.models.enums import (
    ApprovalStatus,
    EdgeKind,
    NodeKind,
    RunState,
    Severity,
)

#: Weights from §18. Named rather than inlined so the formula string below and
#: the arithmetic cannot drift apart.
BREAKING_PENALTY: Final = 15
NON_BREAKING_PENALTY: Final = 5
APPROVAL_PENALTY: Final = 20
COVERAGE_WEIGHT: Final = 20

#: ASCII throughout: this string is rendered in a browser and read aloud by
#: screen readers, and the typographic minus is a different character from the
#: one in the arithmetic above it.
FORMULA: Final = (
    "health = 100 - 15*(relevant unresolved breaking) "
    "- 5*(relevant unresolved non-breaking) "
    "- 20*(pending high-risk approvals) - coverage_penalty; "
    "coverage_penalty = round(20 * uncovered / total integration points), "
    "0 when there are none; clamped to [0, 100]"
)

#: Run states that mean a change is still outstanding. A merged migration is
#: resolved; one waiting on a person is not.
_UNRESOLVED: Final = frozenset(
    {
        RunState.CHANGE_RELEVANT,
        RunState.REHEARSAL_PENDING,
        RunState.REHEARSAL_RUNNING,
        RunState.REHEARSAL_CONFIRMED,
        RunState.REHEARSAL_UNAVAILABLE,
        RunState.MIGRATION_PENDING,
        RunState.MIGRATION_RUNNING,
        RunState.PATCH_READY,
        RunState.VALIDATION_RUNNING,
        RunState.VALIDATION_PASSED,
        RunState.VALIDATION_FAILED,
        RunState.REPAIR_RUNNING,
        RunState.SECURITY_REVIEW_RUNNING,
        RunState.SECURITY_REVIEW_PASSED,
        RunState.SECURITY_REVIEW_FAILED,
        RunState.APPROVAL_PENDING,
        RunState.PR_PENDING,
        RunState.PR_CREATING,
        RunState.PR_CREATED,
        RunState.MERGE_WAITING,
        RunState.HUMAN_REVIEW_REQUIRED,
    }
)

_HIGH_RISK: Final = frozenset({Severity.HIGH, Severity.CRITICAL})


@dataclass(slots=True)
class IntegrationHealth:
    """A score, or an honest statement that there is not one yet."""

    available: bool
    score: int | None = None
    formula: str = FORMULA
    inputs: dict[str, Any] = field(default_factory=dict)
    unavailable_reason: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "score": self.score,
            "formula": self.formula if self.available else None,
            "inputs": dict(self.inputs),
            "unavailable_reason": self.unavailable_reason,
        }


async def integration_health(
    session: AsyncSession, project: Project
) -> IntegrationHealth:
    """Compute the score from rows, or explain why there is not one."""
    version = (
        await session.execute(
            select(func.max(GraphNode.graph_version)).where(
                GraphNode.project_id == project.id
            )
        )
    ).scalar_one_or_none()

    if version is None:
        return IntegrationHealth(
            available=False,
            unavailable_reason=(
                "this project has not been scanned, so it has no integration "
                "points to score"
            ),
        )

    total, uncovered = await _coverage(session, project.id, version)
    breaking, non_breaking = await _unresolved_changes(session, project.id)
    approvals = await _pending_high_risk(session, project.id)

    coverage_penalty = round(COVERAGE_WEIGHT * uncovered / total) if total else 0
    raw = (
        100
        - BREAKING_PENALTY * breaking
        - NON_BREAKING_PENALTY * non_breaking
        - APPROVAL_PENALTY * approvals
        - coverage_penalty
    )

    return IntegrationHealth(
        available=True,
        score=max(0, min(100, raw)),
        inputs={
            "integration_points": total,
            "uncovered_integration_points": uncovered,
            "coverage_penalty": coverage_penalty,
            "relevant_unresolved_breaking": breaking,
            "relevant_unresolved_non_breaking": non_breaking,
            "pending_high_risk_approvals": approvals,
            "graph_version": version,
        },
    )


async def _coverage(
    session: AsyncSession, project_id: uuid.UUID, version: int
) -> tuple[int, int]:
    """Integration points, and how many no test covers.

    An integration point is a call site. "Covered" means the symbol it lives in
    is reachable from a `COVERED_BY_TEST` edge — which is the same relationship
    `blast_radius` traverses, so the score and the impact report agree about
    what a test covers.
    """
    call_sites = list(
        (
            await session.execute(
                select(GraphNode).where(
                    GraphNode.project_id == project_id,
                    GraphNode.graph_version == version,
                    GraphNode.kind == NodeKind.CALL_SITE,
                )
            )
        ).scalars()
    )
    total = len(call_sites)
    if not total:
        return 0, 0

    edges = list(
        (
            await session.execute(
                select(GraphEdge).where(
                    GraphEdge.project_id == project_id,
                    GraphEdge.graph_version == version,
                )
            )
        ).scalars()
    )

    defined_in = {
        edge.source_node_id: edge.target_node_id
        for edge in edges
        if edge.kind is EdgeKind.DEFINED_IN
    }
    covered_symbols = {
        edge.source_node_id for edge in edges if edge.kind is EdgeKind.COVERED_BY_TEST
    }

    uncovered = sum(
        1
        for site in call_sites
        if defined_in.get(site.id) not in covered_symbols
    )
    return total, uncovered


async def _unresolved_changes(
    session: AsyncSession, project_id: uuid.UUID
) -> tuple[int, int]:
    """Relevant changes still outstanding, split by whether they break callers."""
    rows = list(
        (
            await session.execute(
                select(ChangeEvent.breaking)
                .join(MigrationRun, MigrationRun.change_event_id == ChangeEvent.id)
                .where(
                    MigrationRun.project_id == project_id,
                    MigrationRun.state.in_(_UNRESOLVED),
                )
            )
        ).scalars()
    )
    breaking = sum(1 for value in rows if value)
    return breaking, len(rows) - breaking


async def _pending_high_risk(session: AsyncSession, project_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Approval)
            .where(
                Approval.project_id == project_id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.risk.in_(_HIGH_RISK),
            )
        )
    ).scalar_one()
