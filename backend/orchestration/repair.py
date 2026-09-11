"""C7-04: PATCH → TEST → FAIL → DIAGNOSE → REPAIR → RETEST, bounded.

The product's central behaviour, and the place where "autonomous" has to stop
meaning "unbounded". Three things enforce the bound, none of them the model:

* the loop counts `migration_attempts` rows, so a restart mid-run cannot hand
  itself a fresh budget;
* the table's unique constraint on `(migration_run_id, attempt_number)` refuses
  a duplicate even under a race;
* on exhaustion the run reaches `HUMAN_REVIEW_REQUIRED` — a state from which the
  only moves are human ones.

Each attempt records what `02_ARCHITECTURE.md` §12 asks for: attempt number,
failure evidence, diagnosis, files modified, tests executed, outcome. The next
attempt is given the previous failure, so a repair is a response to evidence
rather than another roll of the dice.

A blocking finding stops the loop rather than being retried. A patch that
introduces a credential or a dependency is not a failure to repair around; it is
a decision for a person.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.contracts import AnalyzedChange, ImpactAnalystOutput
from backend.agents.migration_engineer import MigrationPatch, produce_patch
from backend.agents.validator import Validation, validate
from backend.migrations.workspace import MigrationWorkspace
from backend.models import MigrationAttempt, MigrationRun, SecurityFinding
from backend.models.enums import ActivityEventKind, AttemptOutcome, RunState
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.coordinator import RunCoordinator
from backend.shared.model_provider import ModelProvider
from backend.validation.discovery import TestCommandNotFound
from backend.validation.results import UnparseableTestOutput
from backend.validation.runner import run_tests, store_result

logger = get_logger(__name__)


@dataclass(slots=True)
class AttemptRecord:
    """One pass through the loop, as it happened."""

    attempt_number: int
    outcome: AttemptOutcome
    patch: MigrationPatch | None = None
    validation: Validation | None = None
    diagnosis: str = ""
    blocked_by: list[str] = field(default_factory=list)
    #: Whether this attempt got as far as running the suite. An attempt that
    #: produced no patch never did, and must not be routed through the
    #: validation-failure path — there was no validation.
    reached_validation: bool = False

    @property
    def passed(self) -> bool:
        return self.outcome is AttemptOutcome.PASSED


@dataclass(slots=True)
class RepairResult:
    """What the whole loop did."""

    final_state: RunState
    attempts: list[AttemptRecord] = field(default_factory=list)
    reason: str = ""

    @property
    def repaired(self) -> bool:
        return self.final_state is RunState.VALIDATION_PASSED

    @property
    def attempts_used(self) -> int:
        return len(self.attempts)


async def run_repair_loop(
    session: AsyncSession,
    run: MigrationRun,
    workspace: MigrationWorkspace,
    *,
    change: AnalyzedChange,
    impact: ImpactAnalystOutput,
    impact_set: list[str],
    model_provider: ModelProvider,
    coordinator: RunCoordinator,
    max_attempts: int,
    test_selectors: list[str] | None = None,
) -> RepairResult:
    """Patch, test, and repair until it passes or the budget runs out."""
    result = RepairResult(final_state=run.state)
    previous_failure: str | None = None

    while True:
        used = await _attempts_used(session, run)
        if used >= max_attempts:
            # Checked before starting, not after finishing: the budget is a
            # cap on attempts made, and an off-by-one here is an extra run of
            # a model against the user's code.
            await coordinator.escalate(
                session,
                run,
                reason=(
                    f"Repair budget of {max_attempts} attempts is exhausted. "
                    f"Last failure: {previous_failure or 'unknown'}"
                ),
            )
            result.final_state = RunState.HUMAN_REVIEW_REQUIRED
            result.reason = f"exhausted after {used} attempt(s)"
            return result

        attempt_number = used + 1
        record = await _one_attempt(
            session,
            run,
            workspace,
            change=change,
            impact=impact,
            impact_set=impact_set,
            model_provider=model_provider,
            coordinator=coordinator,
            attempt_number=attempt_number,
            previous_failure=previous_failure,
            test_selectors=test_selectors or [],
        )
        result.attempts.append(record)

        if record.outcome is AttemptOutcome.PASSED:
            result.final_state = RunState.VALIDATION_PASSED
            result.reason = "tests passed"
            return result

        if record.outcome is AttemptOutcome.ESCALATED:
            result.final_state = RunState.HUMAN_REVIEW_REQUIRED
            result.reason = "; ".join(record.blocked_by) or "escalated"
            return result

        previous_failure = _failure_brief(record)

        if not record.reached_validation:
            # No patch was produced, so no validation happened and there is no
            # validation failure to route. The run stays in the state it is
            # already patching from and tries again; the budget check at the top
            # of the loop is what stops it.
            continue

        # The coordinator decides whether another attempt is allowed, by
        # counting rows. A model that has just failed does not get a say in
        # whether it is tried again.
        run = await coordinator.route_validation_failure(
            session, run, failure_summary=previous_failure
        )
        if run.state is RunState.HUMAN_REVIEW_REQUIRED:
            result.final_state = RunState.HUMAN_REVIEW_REQUIRED
            result.reason = f"exhausted after {len(result.attempts)} attempt(s)"
            return result


async def _one_attempt(
    session: AsyncSession,
    run: MigrationRun,
    workspace: MigrationWorkspace,
    *,
    change: AnalyzedChange,
    impact: ImpactAnalystOutput,
    impact_set: list[str],
    model_provider: ModelProvider,
    coordinator: RunCoordinator,
    attempt_number: int,
    previous_failure: str | None,
    test_selectors: list[str],
) -> AttemptRecord:
    await _advance_to_patching(session, run, coordinator, attempt_number)

    try:
        patch = await produce_patch(
            workspace,
            provider_id=run.provider_id,
            from_version=run.from_version,
            to_version=run.to_version,
            change=change,
            impact=impact,
            impact_set=impact_set,
            model_provider=model_provider,
            attempt_number=attempt_number,
            previous_failure=previous_failure,
        )
    except Exception as failure:
        # A model that errors is a failed attempt, not a crashed run. Letting
        # this propagate would abandon the migration mid-flight with the
        # workspace half-patched and no attempt row explaining why — and it
        # would bypass the budget entirely, since nothing would be recorded.
        logger.warning(
            "continuity.migration_engineer_failed",
            extra={"attempt": attempt_number, "error": type(failure).__name__},
        )
        record = AttemptRecord(
            attempt_number=attempt_number,
            outcome=AttemptOutcome.FAILED,
            blocked_by=[f"the engineer failed: {type(failure).__name__}"],
            reached_validation=False,
        )
        await _store_attempt(session, run, record)
        return record

    blocking = patch.blocking_findings
    await _store_findings(session, run, patch)

    if blocking:
        # Not a failure to repair around. A credential in the patch or a new
        # dependency is a decision for a person, and retrying would just
        # produce it again.
        record = AttemptRecord(
            attempt_number=attempt_number,
            outcome=AttemptOutcome.ESCALATED,
            patch=patch,
            blocked_by=[finding.summary for finding in blocking],
        )
        await _store_attempt(session, run, record)
        await coordinator.escalate(
            session,
            run,
            reason=f"{len(blocking)} blocking finding(s) in the proposed patch",
        )
        return record

    if patch.is_empty:
        # A spent attempt, not the end of the run. The suite is deliberately not
        # run — validating an unchanged workspace would report a pass and call
        # the migration done — but an empty response is a transient model
        # failure of the same kind as a refused structured output, and burning
        # the whole run on the first one wastes the budget that exists for
        # exactly this. The check at the top of the loop escalates once the
        # budget is genuinely gone.
        record = AttemptRecord(
            attempt_number=attempt_number,
            outcome=AttemptOutcome.FAILED,
            patch=patch,
            blocked_by=["the engineer produced no applicable edit"],
            reached_validation=False,
        )
        await _store_attempt(session, run, record)
        return record

    # `PATCH_READY` belongs to the first pass only. A repair patches *inside*
    # `REPAIR_RUNNING` and goes straight to validation — `ALLOWED_TRANSITIONS`
    # has no `repair_running -> patch_ready` edge, and that is the right shape:
    # a re-patch is part of the repair, not a fresh migration.
    if run.state is RunState.MIGRATION_RUNNING:
        await coordinator.advance(
            session, run, to_state=RunState.PATCH_READY,
            reason=patch.plan_summary[:500], actor="migration_engineer",
            detail={"attempt": attempt_number, "files": len(patch.applied)},
        )
    await coordinator.advance(
        session, run, to_state=RunState.VALIDATION_RUNNING,
        reason="running the project's tests against the patch", actor="validator",
    )
    await events.emit(
        session,
        kind=ActivityEventKind.VALIDATION_STARTED,
        actor="validator",
        summary=f"Attempt {attempt_number}: running tests",
        project_id=run.project_id,
        migration_run_id=run.id,
    )

    try:
        parsed = await run_tests(workspace, selectors=test_selectors)
    except (TestCommandNotFound, UnparseableTestOutput) as failure:
        # Neither is a failing test suite, and neither may be reported as one.
        record = AttemptRecord(
            attempt_number=attempt_number,
            outcome=AttemptOutcome.ESCALATED,
            patch=patch,
            blocked_by=[f"{failure.code}: {failure}"],
        )
        await _store_attempt(session, run, record)
        await coordinator.escalate(session, run, reason=str(failure))
        return record

    validation = await validate(parsed, model_provider=model_provider)
    await store_result(session, parsed, migration_run_id=run.id)

    record = AttemptRecord(
        attempt_number=attempt_number,
        outcome=AttemptOutcome.PASSED if validation.passed else AttemptOutcome.FAILED,
        patch=patch,
        validation=validation,
        reached_validation=True,
        diagnosis=(
            (validation.interpretation.failure_summary or "")
            if validation.interpretation
            else ""
        ),
    )
    await _store_attempt(session, run, record)

    await coordinator.advance(
        session,
        run,
        to_state=RunState.VALIDATION_PASSED if validation.passed else RunState.VALIDATION_FAILED,
        reason=(
            f"{parsed.passed} passed / {parsed.failed} failed"
            + (" (timed out)" if parsed.timed_out else "")
        ),
        actor="validator",
        detail={"attempt": attempt_number},
    )
    await events.emit(
        session,
        kind=ActivityEventKind.VALIDATION_PASSED
        if validation.passed
        else ActivityEventKind.VALIDATION_FAILED,
        actor="validator",
        summary=(
            f"Attempt {attempt_number}: {parsed.passed} passed, {parsed.failed} failed"
        ),
        project_id=run.project_id,
        migration_run_id=run.id,
    )
    return record


async def _advance_to_patching(
    session: AsyncSession,
    run: MigrationRun,
    coordinator: RunCoordinator,
    attempt_number: int,
) -> None:
    """Enter the state this attempt runs in.

    The first attempt patches from `MIGRATION_PENDING`; later ones are already
    in `REPAIR_RUNNING`, put there by the coordinator when it decided another
    attempt was allowed.
    """
    if run.state is RunState.MIGRATION_PENDING:
        await coordinator.advance(
            session,
            run,
            to_state=RunState.MIGRATION_RUNNING,
            reason=f"attempt {attempt_number}: producing a patch",
            actor="migration_engineer",
        )
        await events.emit(
            session,
            kind=ActivityEventKind.MIGRATION_STARTED,
            actor="migration_engineer",
            summary=f"Attempt {attempt_number}: producing a patch",
            project_id=run.project_id,
            migration_run_id=run.id,
        )


async def _attempts_used(session: AsyncSession, run: MigrationRun) -> int:
    """Counted from rows, so a restart cannot reset the budget."""
    return (
        await session.execute(
            select(func.count())
            .select_from(MigrationAttempt)
            .where(MigrationAttempt.migration_run_id == run.id)
        )
    ).scalar_one()


async def _store_attempt(
    session: AsyncSession, run: MigrationRun, record: AttemptRecord
) -> uuid.UUID:
    patch = record.patch
    validation = record.validation

    row = MigrationAttempt(
        migration_run_id=run.id,
        attempt_number=record.attempt_number,
        plan_summary=patch.plan_summary if patch else None,
        files_changed={"paths": sorted(patch.applied)} if patch else None,
        patch_diff=patch.diff if patch else None,
        commands_executed={"commands": []},
        tests_executed=(
            {"results": [validation.parsed.summary()]} if validation else None
        ),
        failure_evidence=(
            validation.failure_evidence()
            if validation and not validation.passed
            else ({"blocked_by": record.blocked_by} if record.blocked_by else None)
        ),
        diagnosis_summary=record.diagnosis or None,
        outcome=record.outcome,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _store_findings(
    session: AsyncSession, run: MigrationRun, patch: MigrationPatch
) -> None:
    for finding in patch.findings:
        session.add(
            SecurityFinding(
                migration_run_id=run.id,
                category=finding.category,
                severity=finding.severity,
                summary=finding.summary,
                evidence=finding.evidence().model_dump(mode="json"),
                # The patch inspector is deterministic, so its recommendation
                # and the policy decision are the same thing here. They are
                # stored separately because the Security Reviewer agent (C8-01)
                # will disagree with policy eventually, and that disagreement
                # has to be visible.
                recommendation=finding.policy_decision,
                policy_decision=finding.policy_decision,
            )
        )
    if patch.findings:
        await session.flush()


def _rejection_brief(record: AttemptRecord) -> list[str]:
    """Which of the last attempt's edits were discarded, and why.

    Fed back deliberately. Without it the next attempt learns only that tests
    failed, not that its edits never reached disk — so it can propose the same
    rejected edit again and spend the whole budget doing it. A rejection is
    evidence, and this loop is supposed to respond to evidence.
    """
    if record.patch is None or not record.patch.rejected:
        return []

    return [
        "Your previous edits to these files were DISCARDED and never applied. "
        "Do not propose them again unless you address the reason:",
        *(
            f"- {rejected.path}: {rejected.reason}"
            for rejected in record.patch.rejected
        ),
    ]


def _failure_brief(record: AttemptRecord) -> str:
    """What the next attempt is told about this one.

    Counts and failing ids, the diagnosis if a model produced one, and any edits
    that were rejected — enough to respond to, and not the whole suite's output.
    """
    if record.validation is None:
        return "\n".join(
            [*(record.blocked_by or ["no validation result"]), *_rejection_brief(record)]
        )

    parts = [
        f"attempt {record.attempt_number}: "
        f"{record.validation.parsed.passed} passed, "
        f"{record.validation.parsed.failed} failed"
    ]
    parts.extend(_rejection_brief(record))
    parsed = record.validation.parsed
    if parsed.failing_test_ids:
        parts.append("failing: " + ", ".join(parsed.failing_test_ids[:10]))
    if record.diagnosis:
        parts.append(f"diagnosis: {record.diagnosis}")
    excerpt = parsed.output_excerpt.strip()
    if excerpt:
        parts.append("output:\n" + excerpt[-2_000:])
    return "\n".join(parts)
