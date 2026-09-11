"""Deterministic workflow state machine.

`ALLOWED_TRANSITIONS` is the executable form of the edge list in
`02_ARCHITECTURE.md` §8. A test parses that document and compares the two edge
sets **in both directions** — an edge here that is not documented fails just as
loudly as a documented edge that is not implemented. The document and the code
cannot drift.

Model output never moves a run. `transition()` is not exposed as a tool, and the
coordinator calls it only after a validated structured result has already been
turned into a decision by ordinary code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import RunState, StateTransition
from backend.models.schemas import TransitionEvidence
from backend.shared.errors import IllegalTransition

S = RunState

# Escape states are reachable from every non-terminal state. They are added
# programmatically below rather than repeated on 40 rows, which would be both
# noisy and easy to get subtly wrong.
ESCAPE_STATES: Final = frozenset({S.HUMAN_REVIEW_REQUIRED, S.RUN_FAILED})

#: Terminal per run. Each returns the *project* to MONITORING_ACTIVE — a run
#: ending never stops monitoring.
TERMINAL_STATES: Final = frozenset(
    {
        S.CHANGE_IRRELEVANT,
        S.REJECTED,
        S.VERIFIED,
        S.HUMAN_REVIEW_REQUIRED,
        S.RUN_FAILED,
    }
)

_EXPLICIT: Final[dict[RunState, frozenset[RunState]]] = {
    # --- Project lifecycle ---
    S.PROJECT_CREATED: frozenset({S.GITHUB_CONNECTED}),
    S.GITHUB_CONNECTED: frozenset({S.REPOSITORY_SELECTED}),
    S.REPOSITORY_SELECTED: frozenset({S.INITIAL_SCAN_PENDING}),
    S.INITIAL_SCAN_PENDING: frozenset({S.INITIAL_SCAN_RUNNING}),
    S.INITIAL_SCAN_RUNNING: frozenset({S.INITIAL_SCAN_COMPLETE, S.RUN_FAILED}),
    S.INITIAL_SCAN_COMPLETE: frozenset({S.INTEGRATION_MAPPING_RUNNING}),
    S.INTEGRATION_MAPPING_RUNNING: frozenset(
        {S.INTEGRATION_MAPPING_COMPLETE, S.RUN_FAILED}
    ),
    S.INTEGRATION_MAPPING_COMPLETE: frozenset({S.MONITORING_ACTIVE}),
    S.MONITORING_ACTIVE: frozenset({S.CHANGE_DETECTED, S.INITIAL_SCAN_PENDING}),
    # --- Change evaluation ---
    S.CHANGE_DETECTED: frozenset({S.CHANGE_ANALYSIS_RUNNING}),
    S.CHANGE_ANALYSIS_RUNNING: frozenset({S.CHANGE_ANALYSIS_COMPLETE, S.RUN_FAILED}),
    S.CHANGE_ANALYSIS_COMPLETE: frozenset({S.IMPACT_ANALYSIS_RUNNING}),
    S.IMPACT_ANALYSIS_RUNNING: frozenset(
        {S.CHANGE_IRRELEVANT, S.CHANGE_RELEVANT, S.RUN_FAILED}
    ),
    S.CHANGE_IRRELEVANT: frozenset({S.MONITORING_ACTIVE}),
    # MONITORING_ACTIVE is how a project gets back to watching once its
    # relevant changes have been dealt with. Without it a project that
    # ever had a relevant change was stuck forever: the scheduler skips
    # CHANGE_RELEVANT, so it would never be monitored again. Mirrors the
    # CHANGE_IRRELEVANT edge, which always existed.
    S.CHANGE_RELEVANT: frozenset({S.MONITORING_ACTIVE, S.REHEARSAL_PENDING}),
    # --- Rehearsal ---
    S.REHEARSAL_PENDING: frozenset({S.REHEARSAL_RUNNING, S.REHEARSAL_UNAVAILABLE}),
    S.REHEARSAL_RUNNING: frozenset(
        {S.REHEARSAL_CONFIRMED, S.REHEARSAL_FAILED, S.REHEARSAL_UNAVAILABLE}
    ),
    S.REHEARSAL_CONFIRMED: frozenset({S.MIGRATION_PENDING}),
    S.REHEARSAL_UNAVAILABLE: frozenset({S.MIGRATION_PENDING}),
    S.REHEARSAL_FAILED: frozenset({S.HUMAN_REVIEW_REQUIRED, S.MONITORING_ACTIVE}),
    # --- Migration and validation ---
    S.MIGRATION_PENDING: frozenset({S.MIGRATION_RUNNING}),
    S.MIGRATION_RUNNING: frozenset(
        {S.PATCH_READY, S.HUMAN_REVIEW_REQUIRED, S.RUN_FAILED}
    ),
    S.PATCH_READY: frozenset({S.VALIDATION_RUNNING}),
    S.VALIDATION_RUNNING: frozenset(
        {S.VALIDATION_PASSED, S.VALIDATION_FAILED, S.RUN_FAILED}
    ),
    S.VALIDATION_FAILED: frozenset({S.REPAIR_RUNNING, S.HUMAN_REVIEW_REQUIRED}),
    S.REPAIR_RUNNING: frozenset({S.VALIDATION_RUNNING, S.HUMAN_REVIEW_REQUIRED}),
    S.VALIDATION_PASSED: frozenset({S.SECURITY_REVIEW_RUNNING}),
    # --- Security and approval ---
    S.SECURITY_REVIEW_RUNNING: frozenset(
        {S.SECURITY_REVIEW_PASSED, S.SECURITY_REVIEW_FAILED, S.APPROVAL_PENDING}
    ),
    S.SECURITY_REVIEW_FAILED: frozenset({S.REPAIR_RUNNING, S.HUMAN_REVIEW_REQUIRED}),
    S.SECURITY_REVIEW_PASSED: frozenset(
        {S.FINAL_VALIDATION_RUNNING, S.APPROVAL_PENDING}
    ),
    S.APPROVAL_PENDING: frozenset({S.APPROVED, S.REJECTED}),
    S.APPROVED: frozenset({S.FINAL_VALIDATION_RUNNING}),
    S.REJECTED: frozenset({S.MONITORING_ACTIVE}),
    # --- Delivery and verification ---
    S.FINAL_VALIDATION_RUNNING: frozenset(
        {S.FINAL_VALIDATION_PASSED, S.VALIDATION_FAILED, S.RUN_FAILED}
    ),
    S.FINAL_VALIDATION_PASSED: frozenset({S.PR_PENDING}),
    S.PR_PENDING: frozenset({S.PR_CREATING}),
    S.PR_CREATING: frozenset({S.PR_CREATED, S.RUN_FAILED}),
    S.PR_CREATED: frozenset({S.MERGE_WAITING}),
    S.MERGE_WAITING: frozenset(
        {S.POST_MERGE_VERIFICATION_RUNNING, S.VERIFIED, S.MONITORING_ACTIVE}
    ),
    S.POST_MERGE_VERIFICATION_RUNNING: frozenset(
        {S.POST_MERGE_VERIFICATION_PASSED, S.POST_MERGE_VERIFICATION_FAILED}
    ),
    S.POST_MERGE_VERIFICATION_PASSED: frozenset({S.VERIFIED}),
    S.POST_MERGE_VERIFICATION_FAILED: frozenset({S.HUMAN_REVIEW_REQUIRED}),
    S.VERIFIED: frozenset({S.MONITORING_ACTIVE}),
    # --- Escape states ---
    S.HUMAN_REVIEW_REQUIRED: frozenset({S.MIGRATION_PENDING, S.MONITORING_ACTIVE}),
    S.RUN_FAILED: frozenset({S.MONITORING_ACTIVE}),
}


def _build() -> dict[RunState, frozenset[RunState]]:
    """Add the escape edges every non-terminal state carries."""
    table: dict[RunState, frozenset[RunState]] = {}
    for state in RunState:
        targets = _EXPLICIT.get(state, frozenset())
        if state not in TERMINAL_STATES:
            targets = targets | ESCAPE_STATES
        table[state] = targets
    return table


#: state → states reachable in one step.
ALLOWED_TRANSITIONS: Final[dict[RunState, frozenset[RunState]]] = _build()


def can_transition(from_state: RunState, to_state: RunState) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


async def transition(
    session: AsyncSession,
    *,
    from_state: RunState,
    to_state: RunState,
    evidence: TransitionEvidence,
    project_id: uuid.UUID | None = None,
    migration_run_id: uuid.UUID | None = None,
) -> StateTransition:
    """Move a run and record why, in one transaction.

    Raises `IllegalTransition` if the edge does not exist. The caller updates the
    owning row's `state` inside the same transaction, so a recorded transition
    and the state it produced can never disagree.
    """
    if not can_transition(from_state, to_state):
        raise IllegalTransition(from_state.value, to_state.value)

    record = StateTransition(
        project_id=project_id,
        migration_run_id=migration_run_id,
        from_state=from_state,
        to_state=to_state,
        actor=evidence.actor,
        evidence=evidence.model_dump(mode="json"),
        occurred_at=datetime.now(UTC),
    )
    session.add(record)
    await session.flush()
    return record
