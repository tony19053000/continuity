"""C6-04: prove the incompatibility before touching anyone's code.

Continuity does not migrate a project because a provider *said* something
changed. It runs the project's own affected tests against both sides of the
contract and looks at the difference. If there is no difference, there is
nothing to fix, and the run stops.

**Nothing here is model-generated.** Pass counts come from parsed process
output, the tests are selected from the graph, and the decision is arithmetic on
two integers. A model cannot report a test as passing, which is the whole point
of `02_ARCHITECTURE.md` §13.

Three outcomes, and the state machine allows exactly the right moves from each:

* `REHEARSAL_CONFIRMED` — the new contract breaks tests the old one passed.
  The evidence a migration is built on. Proceeds to `MIGRATION_PENDING`.
* `REHEARSAL_UNAVAILABLE` — the rehearsal could not be run at all: no usable
  spec for one of the versions, or no test covers the affected code. The reason
  is stored, and the run **still proceeds** to `MIGRATION_PENDING`, because "we
  could not check" is not "nothing is wrong".
* `REHEARSAL_FAILED` — the rehearsal ran and reproduced no difference. The run
  escalates instead of migrating. `ALLOWED_TRANSITIONS` has no edge from here to
  `MIGRATION_PENDING`, so a patch cannot be built on a difference nobody could
  reproduce.

Simulating a provider is out of scope for this repository (`CLAUDE.md` §3.13).
What lives here is the harness and the `RehearsalAdapter` interface a simulator
plugs into; `NoSimulationAdapter` is the honest default, and it reports that it
cannot simulate rather than running both sides identically and calling the
resulting non-difference a result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import MigrationRun, TestResult
from backend.models.enums import ActivityEventKind, RunState
from backend.models.schemas import ProviderChange, TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.providers.storage import latest_spec
from backend.shared.execution import CommandResult, CommandSpec, ExecutionProvider

logger = get_logger(__name__)

#: pytest's summary line: `= 3 failed, 11 passed, 1 skipped in 0.42s =`.
_PYTEST_COUNT: Final = re.compile(
    r"(?P<count>\d+)\s+(?P<outcome>passed|failed|error|errors|skipped|xfailed|xpassed)"
)

#: `FAILED tests/test_pay.py::test_charge - AssertionError`
_PYTEST_FAILURE: Final = re.compile(r"^(?:FAILED|ERROR)\s+(?P<test_id>\S+)", re.MULTILINE)

MAX_OUTPUT_EXCERPT: Final = 4_000


class ContractSide(StrEnum):
    """Which version of the provider contract the tests ran against."""

    OLD = "old"
    NEW = "new"


@dataclass(frozen=True, slots=True)
class SuiteOutcome:
    """Parsed counts from one real execution. Never model-produced."""

    side: ContractSide
    command: str
    exit_code: int | None
    passed: int
    failed: int
    skipped: int
    failing_test_ids: list[str]
    duration_ms: int
    timed_out: bool = False
    output_excerpt: str = ""

    @property
    def ran(self) -> bool:
        """Whether the suite actually executed and reported counts.

        A timeout or a collection error produces no counts, and treating that as
        "zero failures" would read as a pass.
        """
        return not self.timed_out and bool(self.passed or self.failed or self.skipped)


@dataclass(slots=True)
class RehearsalResult:
    """The delta between the two sides, and what it means."""

    outcome: RunState
    reason: str
    old: SuiteOutcome | None = None
    new: SuiteOutcome | None = None
    selected_tests: list[str] = field(default_factory=list)
    affected_workflows: list[str] = field(default_factory=list)
    newly_failing: list[str] = field(default_factory=list)

    @property
    def reproduced_a_difference(self) -> bool:
        return self.outcome is RunState.REHEARSAL_CONFIRMED

    def summary(self) -> dict[str, object]:
        """JSON-safe evidence, stored on the migration run."""

        def side(outcome: SuiteOutcome | None) -> dict[str, object] | None:
            if outcome is None:
                return None
            return {
                "command": outcome.command,
                "exit_code": outcome.exit_code,
                "passed": outcome.passed,
                "failed": outcome.failed,
                "skipped": outcome.skipped,
                "failing_test_ids": list(outcome.failing_test_ids),
                "duration_ms": outcome.duration_ms,
                "timed_out": outcome.timed_out,
            }

        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "old": side(self.old),
            "new": side(self.new),
            "selected_tests": list(self.selected_tests),
            "affected_workflows": list(self.affected_workflows),
            "newly_failing": list(self.newly_failing),
        }


@runtime_checkable
class RehearsalAdapter(Protocol):
    """Makes a workspace behave like one side of a provider contract.

    The seam Provider Lab and externally-built simulators plug into. Continuity
    holds no knowledge of any particular provider's simulation mechanism.
    """

    @property
    def can_simulate(self) -> bool:
        """False when this adapter cannot reproduce either side of the change."""
        ...

    async def prepare(
        self, side: ContractSide, workspace: Path, change: ProviderChange
    ) -> None:
        """Put the workspace into the state for one side of the contract."""
        ...

    def command(self, workspace: Path, selectors: list[str]) -> CommandSpec:
        """The test command to run, as argv. Never a shell string."""
        ...


class NoSimulationAdapter:
    """The honest default: this deployment cannot simulate a provider.

    Running the same suite twice and reporting the inevitable non-difference
    would be a fabricated result — and worse, it would read as
    `REHEARSAL_FAILED`, which says "we checked and found nothing" rather than
    "we could not check".
    """

    can_simulate = False

    async def prepare(
        self, side: ContractSide, workspace: Path, change: ProviderChange
    ) -> None:
        raise NotImplementedError("no provider simulation is configured")

    def command(self, workspace: Path, selectors: list[str]) -> CommandSpec:
        raise NotImplementedError("no provider simulation is configured")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_pytest_output(
    result: CommandResult, *, side: ContractSide
) -> SuiteOutcome:
    """Turn real process output into counts. Deterministic, no model.

    Errors count as failures: a suite that could not import the module it tests
    has not passed, and folding errors into a separate bucket nobody reads is
    how a broken rehearsal looks green.
    """
    text = f"{result.stdout}\n{result.stderr}"
    counts = {"passed": 0, "failed": 0, "skipped": 0}

    for match in _PYTEST_COUNT.finditer(text):
        outcome = match.group("outcome")
        count = int(match.group("count"))
        if outcome in {"error", "errors"}:
            counts["failed"] += count
        elif outcome == "xpassed":
            counts["passed"] += count
        elif outcome == "xfailed":
            counts["skipped"] += count
        elif outcome in counts:
            counts[outcome] = max(counts[outcome], count)

    failing = sorted({m.group("test_id") for m in _PYTEST_FAILURE.finditer(text)})

    return SuiteOutcome(
        side=side,
        command=" ".join(result.argv),
        exit_code=result.exit_code,
        passed=counts["passed"],
        failed=counts["failed"],
        skipped=counts["skipped"],
        failing_test_ids=failing,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        output_excerpt=text[-MAX_OUTPUT_EXCERPT:],
    )


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


async def rehearse(
    session: AsyncSession,
    run: MigrationRun,
    change: ProviderChange,
    *,
    executor: ExecutionProvider,
    adapter: RehearsalAdapter,
    workspace: Path,
    selected_tests: list[str],
    affected_workflows: list[str] | None = None,
) -> RehearsalResult:
    """Run the affected tests against both contract sides and compare.

    `selected_tests` comes from `graph.tests_covering` — the tests that cover
    the code the change reaches, rather than the whole suite. Running everything
    would bury the difference in unrelated noise and cost far more.
    """
    workflows = affected_workflows or []

    unavailable = await _unavailable_reason(session, run, adapter, selected_tests)
    if unavailable is not None:
        result = RehearsalResult(
            outcome=RunState.REHEARSAL_UNAVAILABLE,
            reason=unavailable,
            selected_tests=list(selected_tests),
            affected_workflows=workflows,
        )
        await _record(session, run, result)
        return result

    # The run enters RUNNING before anything executes. Not bookkeeping: the
    # terminal rehearsal states are only reachable from here, so a run that
    # crashes mid-rehearsal is visibly stuck in RUNNING rather than sitting in
    # PENDING as though it had never started.
    await _begin(session, run)

    sides: dict[ContractSide, SuiteOutcome] = {}
    for side in (ContractSide.OLD, ContractSide.NEW):
        await adapter.prepare(side, workspace, change)
        command = adapter.command(workspace, selected_tests)
        execution = await executor.run(command)
        sides[side] = parse_pytest_output(execution, side=side)
        await _store_suite(session, run, sides[side])

    old, new = sides[ContractSide.OLD], sides[ContractSide.NEW]
    result = _compare(old, new, selected_tests, workflows)
    await _record(session, run, result)
    return result


def _compare(
    old: SuiteOutcome,
    new: SuiteOutcome,
    selected_tests: list[str],
    workflows: list[str],
) -> RehearsalResult:
    """Decide the outcome from two integers and two id lists. No judgment."""
    base = RehearsalResult(
        outcome=RunState.REHEARSAL_FAILED,
        reason="",
        old=old,
        new=new,
        selected_tests=list(selected_tests),
        affected_workflows=workflows,
    )

    if not old.ran or not new.ran:
        # No counts from one side means no comparison was made. Reported as
        # unavailable rather than as "no difference found", which would claim a
        # check that never happened.
        base.outcome = RunState.REHEARSAL_UNAVAILABLE
        base.reason = (
            f"the {'old' if not old.ran else 'new'} side produced no test counts "
            f"({'timed out' if (old.timed_out or new.timed_out) else 'no tests ran'})"
        )
        return base

    newly_failing = sorted(set(new.failing_test_ids) - set(old.failing_test_ids))
    base.newly_failing = newly_failing

    if newly_failing or new.failed > old.failed:
        base.outcome = RunState.REHEARSAL_CONFIRMED
        base.reason = (
            f"{new.failed - old.failed} more test(s) fail against "
            f"{ContractSide.NEW.value} contract "
            f"(old: {old.passed} passed / {old.failed} failed, "
            f"new: {new.passed} passed / {new.failed} failed)"
        )
        return base

    base.outcome = RunState.REHEARSAL_FAILED
    base.reason = (
        "the rehearsal ran and reproduced no difference "
        f"(both sides: {old.passed} passed / {old.failed} failed). "
        "Escalated rather than migrating."
    )
    return base


async def _unavailable_reason(
    session: AsyncSession,
    run: MigrationRun,
    adapter: RehearsalAdapter,
    selected_tests: list[str],
) -> str | None:
    """Why the rehearsal cannot run, or None if it can."""
    if not adapter.can_simulate:
        return "no provider simulation adapter is configured for this deployment"

    if not selected_tests:
        return "no test in this repository covers the affected code"

    for version in (run.from_version, run.to_version):
        spec = await latest_spec(session, run.provider_id, version)
        if spec is None:
            # `latest_spec` returns only parseable documents, so this covers
            # both "never fetched" and "fetched but unreadable" — in either case
            # there is no contract to rehearse against.
            return (
                f"no usable {run.provider_id} specification is stored for "
                f"version {version}"
            )
    return None


async def _begin(session: AsyncSession, run: MigrationRun) -> None:
    """Move REHEARSAL_PENDING -> REHEARSAL_RUNNING."""
    tracked = await session.get(MigrationRun, run.id) or run
    if not can_transition(tracked.state, RunState.REHEARSAL_RUNNING):
        return

    await transition(
        session,
        from_state=tracked.state,
        to_state=RunState.REHEARSAL_RUNNING,
        evidence=TransitionEvidence(
            reason="running the affected tests against both contract sides",
            actor="rehearsal",
            detail={},
        ),
        project_id=tracked.project_id,
        migration_run_id=tracked.id,
    )
    tracked.state = RunState.REHEARSAL_RUNNING
    await session.flush()


async def _store_suite(
    session: AsyncSession, run: MigrationRun, outcome: SuiteOutcome
) -> None:
    session.add(
        TestResult(
            migration_run_id=run.id,
            suite=f"rehearsal:{outcome.side.value}",
            command=outcome.command,
            passed=outcome.passed,
            failed=outcome.failed,
            skipped=outcome.skipped,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            failing_test_ids={"ids": list(outcome.failing_test_ids)},
            raw_output_excerpt=outcome.output_excerpt,
        )
    )
    await session.flush()


async def _record(
    session: AsyncSession, run: MigrationRun, result: RehearsalResult
) -> None:
    """Persist the delta as evidence and move the run."""
    tracked = await session.get(MigrationRun, run.id) or run
    tracked.rehearsal = result.summary()

    if can_transition(tracked.state, result.outcome):
        await transition(
            session,
            from_state=tracked.state,
            to_state=result.outcome,
            evidence=TransitionEvidence(
                reason=result.reason,
                actor="rehearsal",
                detail={
                    "selected_tests": len(result.selected_tests),
                    "newly_failing": len(result.newly_failing),
                },
            ),
            project_id=tracked.project_id,
            migration_run_id=tracked.id,
        )
        tracked.state = result.outcome
    else:
        logger.info(
            "continuity.rehearsal_outcome_not_applied",
            extra={
                "migration_run_id": str(tracked.id),
                "from_state": tracked.state.value,
                "to_state": result.outcome.value,
            },
        )

    await session.flush()

    await events.emit(
        session,
        kind=ActivityEventKind.REHEARSAL_CONFIRMED
        if result.reproduced_a_difference
        else ActivityEventKind.REHEARSAL_STARTED,
        actor="rehearsal",
        summary=result.reason,
        project_id=tracked.project_id,
        migration_run_id=tracked.id,
    )

    logger.info(
        "continuity.rehearsal_complete",
        extra={
            "migration_run_id": str(tracked.id),
            "outcome": result.outcome.value,
            "newly_failing": len(result.newly_failing),
        },
    )


async def proceed_after_rehearsal(
    session: AsyncSession, run: MigrationRun, result: RehearsalResult
) -> RunState:
    """Move the run on from a rehearsal outcome.

    Separate from `rehearse` because the moves differ in kind: confirmed and
    unavailable both continue to `MIGRATION_PENDING`, while failed escalates.
    The state machine enforces this regardless — there is no edge from
    `REHEARSAL_FAILED` to `MIGRATION_PENDING` — so this function chooses among
    legal moves rather than being the thing that prevents an illegal one.
    """
    tracked = await session.get(MigrationRun, run.id) or run

    target = (
        RunState.HUMAN_REVIEW_REQUIRED
        if result.outcome is RunState.REHEARSAL_FAILED
        else RunState.MIGRATION_PENDING
    )

    await transition(
        session,
        from_state=tracked.state,
        to_state=target,
        evidence=TransitionEvidence(
            reason=result.reason,
            actor="rehearsal",
            detail={"rehearsal_outcome": result.outcome.value},
        ),
        project_id=tracked.project_id,
        migration_run_id=tracked.id,
    )
    tracked.state = target
    await session.flush()
    return target


def selected_test_ids(tests: list) -> list[str]:  # type: ignore[type-arg]
    """Graph TEST nodes -> selectors, deduplicated and ordered.

    Deterministic ordering matters: the two sides must run the same selectors in
    the same order, or the delta is between two different suites.
    """
    return sorted({node.key for node in tests})
