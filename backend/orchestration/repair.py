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
from backend.agents.security_reviewer import SecurityReview, review_patch
from backend.agents.validator import Validation, validate
from backend.approvals.service import ApprovalRequest, create_request
from backend.migrations.workspace import MigrationWorkspace
from backend.models import MigrationAttempt, MigrationRun, SecurityFinding
from backend.models.enums import (
    ActivityEventKind,
    AttemptOutcome,
    PolicyDecision,
    RunState,
    Severity,
)
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.coordinator import RunCoordinator
from backend.orchestration.delivery_gate import (
    PATCH_DIFF_DIGEST_KEY,
    SECURITY_REVIEW_KEY,
    patch_digest,
)
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
    #: The security review of the patch that passed, when one passed. `None`
    #: when the loop never reached a green suite — there is nothing to review.
    review: SecurityReview | None = None

    @property
    def repaired(self) -> bool:
        """Whether the loop got the suite green.

        Defined by an attempt passing rather than by the final state, because
        the run continues into the security review afterwards — a patch can be
        a successful repair and still be refused delivery, and conflating the
        two would make "repaired" mean "shipped".
        """
        return any(attempt.passed for attempt in self.attempts)

    @property
    def security_passed(self) -> bool:
        return self.final_state is RunState.SECURITY_REVIEW_PASSED

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
            # A green suite is not the end. The patch still has to survive the
            # security review, and that review is what produces the findings a
            # human decides on — so it runs here, in the live path, rather than
            # only in its own tests.
            await _review(
                session,
                run,
                record,
                impact_set=impact_set,
                model_provider=model_provider,
                coordinator=coordinator,
                result=result,
            )
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
    """Findings from the patch rules (C7-02).

    These are deterministic, so the agent's recommendation and the policy
    decision genuinely are the same value — code found it and policy classified
    it, with no second opinion involved. The columns still differ in general:
    the Security Reviewer's findings are written by `_review` below, and those
    carry two independently-produced verdicts.
    """
    for finding in patch.findings:
        session.add(
            SecurityFinding(
                migration_run_id=run.id,
                category=finding.category,
                severity=finding.severity,
                summary=finding.summary,
                evidence=finding.evidence().model_dump(mode="json"),
                recommendation=finding.policy_decision,
                policy_decision=finding.policy_decision,
            )
        )
    if patch.findings:
        await session.flush()


async def _review(
    session: AsyncSession,
    run: MigrationRun,
    record: AttemptRecord,
    *,
    impact_set: list[str],
    model_provider: ModelProvider,
    coordinator: RunCoordinator,
    result: RepairResult,
) -> None:
    """Review the patch that passed, and record what the review found.

    This is where `recommendation` and `policy_decision` become genuinely
    independent: the agent forms a view, the policy engine classifies the
    category, and a disagreement between them is persisted rather than
    reconciled (`03_SECURITY_ACCESS.md` §10).
    """
    patch = record.patch
    if patch is None:  # pragma: no cover - a PASSED attempt always has one
        return

    await coordinator.advance(
        session,
        run,
        to_state=RunState.SECURITY_REVIEW_RUNNING,
        reason="reviewing the validated patch",
        actor="security_reviewer",
    )
    await events.emit(
        session,
        kind=ActivityEventKind.SECURITY_REVIEW_STARTED,
        actor="security_reviewer",
        summary="Reviewing the validated patch",
        project_id=run.project_id,
        migration_run_id=run.id,
    )

    review = await review_patch(
        provider_id=run.provider_id,
        diff=patch.diff,
        changed_dependencies=list(patch.new_dependencies),
        modifies_tests=patch.modifies_tests,
        impact_set=impact_set,
        model_provider=model_provider,
    )
    result.review = review

    for finding in review.findings:
        session.add(
            SecurityFinding(
                migration_run_id=run.id,
                category=finding.category,
                severity=finding.severity,
                summary=finding.summary,
                evidence=finding.evidence.model_dump(mode="json"),
                # Two verdicts, stored separately on purpose. When the agent
                # says allow and policy says deny, both are in the row.
                recommendation=finding.recommendation,
                policy_decision=finding.policy_decision,
            )
        )

    # Record that the review *happened*, and the identity of exactly what it
    # reviewed. Without the first, an empty finding list is ambiguous — clean or
    # never looked at — and the delivery gate cannot fail closed. Without the
    # second, a run resumed after approval could deliver different bytes from
    # the ones a person said yes to.
    await _record_review(session, run, review, patch)
    await session.flush()

    if review.decision is PolicyDecision.ALLOW:
        await coordinator.advance(
            session,
            run,
            to_state=RunState.SECURITY_REVIEW_PASSED,
            reason=review.summary[:500],
            actor="security_reviewer",
            detail={"findings": len(review.findings)},
        )
        result.final_state = RunState.SECURITY_REVIEW_PASSED
        result.reason = "tests passed and the security review found nothing blocking"
        return

    # ASK and DENY both stop here. An unanswered question is not permission, and
    # the difference between them is what the approval card says, not whether
    # the run continues on its own.
    target = (
        RunState.APPROVAL_PENDING
        if review.decision is PolicyDecision.ASK
        else RunState.SECURITY_REVIEW_FAILED
    )
    await coordinator.advance(
        session,
        run,
        to_state=target,
        reason=(
            f"security review returned {review.decision.value}: "
            f"{len(review.blocking)} finding(s) require a decision"
        ),
        actor="security_reviewer",
        detail={
            "decision": review.decision.value,
            "agent_recommendation": review.agent_recommendation.value,
            "disagreements": len(review.disagreements),
        },
    )
    result.final_state = target
    result.reason = f"security review returned {review.decision.value}"

    if target is RunState.APPROVAL_PENDING:
        # A run that pauses without an approval request pauses forever: there
        # is nothing for a person to answer, and nothing to resume from.
        await _request_approval(session, run, review, patch)

    if review.disagreements:
        logger.warning(
            "continuity.security_review_disagreement_persisted",
            extra={
                "migration_run_id": str(run.id),
                "count": len(review.disagreements),
            },
        )


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


async def _record_review(
    session: AsyncSession,
    run: MigrationRun,
    review: SecurityReview,
    patch: MigrationPatch,
) -> None:
    """Store the review and the patch identity on the run.

    Written into `evidence_report` alongside the impact assessment rather than a
    new column, because both answer the same question — what is this run's
    recorded evidence — and the report builder already reads from there.
    """
    tracked = await session.get(MigrationRun, run.id) or run
    stored = dict(tracked.evidence_report or {})
    stored[SECURITY_REVIEW_KEY] = review.report()
    # The diff's identity. The *files* digest is recorded by the caller that
    # holds the workspace, because only it can read what the patch produced.
    stored[PATCH_DIFF_DIGEST_KEY] = patch_digest(patch.diff)
    tracked.evidence_report = stored
    await session.flush()


async def _request_approval(
    session: AsyncSession,
    run: MigrationRun,
    review: SecurityReview,
    patch: MigrationPatch,
) -> None:
    """Ask a person, in a row they can actually answer.

    The request carries what a reviewer needs to decide without reading the
    code: which findings blocked, what the agent advised, what policy ruled, and
    the identity of the patch — so an approval granted for one patch cannot
    later authorise a different one.
    """
    blocking = review.blocking
    risk = max(
        (finding.severity for finding in blocking),
        default=Severity.MEDIUM,
        key=_SEVERITY_ORDER.index,
    )

    approval = await create_request(
        session,
        ApprovalRequest(
            project_id=run.project_id,
            migration_run_id=run.id,
            trigger=blocking[0].category.value if blocking else "security_review",
            risk=risk,
            requested_action={
                "migration": f"{run.provider_id} {run.from_version} -> {run.to_version}",
                "files": sorted(patch.applied),
                PATCH_DIFF_DIGEST_KEY: patch_digest(patch.diff),
                "findings": [finding.summary_dict() for finding in blocking],
                "agent_recommendation": review.agent_recommendation.value,
                "policy_decision": review.decision.value,
            },
            agent_recommendation=review.agent_recommendation.value,
        ),
    )

    await events.emit(
        session,
        kind=ActivityEventKind.APPROVAL_REQUIRED,
        actor="policy",
        summary=(
            f"{len(blocking)} finding(s) require a decision before this "
            "migration can be delivered."
        ),
        project_id=run.project_id,
        migration_run_id=run.id,
    )

    logger.info(
        "continuity.approval_requested",
        extra={
            "migration_run_id": str(run.id),
            "approval_id": str(approval.id),
            "risk": risk.value,
            "findings": len(blocking),
        },
    )


#: Severity order, weakest first, for picking a request's headline risk.
_SEVERITY_ORDER = [
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
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
