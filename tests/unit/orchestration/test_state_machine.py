"""C2-04 acceptance: the state machine is deterministic and matches §8 exactly.

The document-parity test is the important one. `02_ARCHITECTURE.md` §8 is
declared the specification, so it is parsed and compared against
`ALLOWED_TRANSITIONS` in both directions — an undocumented edge in the code is
as much a failure as an unimplemented documented edge.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from backend.models import RunState, StateTransition
from backend.models.schemas import TransitionEvidence
from backend.models.session import session_scope
from backend.orchestration.state_machine import (
    ALLOWED_TRANSITIONS,
    ESCAPE_STATES,
    TERMINAL_STATES,
    can_transition,
    transition,
)
from backend.shared.errors import IllegalTransition

ARCHITECTURE = Path(__file__).resolve().parents[3] / "02_ARCHITECTURE.md"


def _documented_edges() -> set[tuple[RunState, RunState]]:
    """Parse the §8 edge tables.

    Rows look like `| FROM_STATE | TO_A, TO_B |`. Only rows whose first cell is a
    known state are taken, which skips the header and prose rows.
    """
    text = ARCHITECTURE.read_text()
    section = text.split("## 8. Orchestration state machine", 1)[1]
    section = section.split("\n## 9.", 1)[0]

    by_name = {state.name: state for state in RunState}
    edges: set[tuple[RunState, RunState]] = set()

    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] not in by_name:
            continue
        source = by_name[cells[0]]
        for target_name in re.split(r",\s*", cells[1]):
            # Pull the SCREAMING_CASE identifier out directly. Cells carry
            # backticks and parenthetical notes — "`INITIAL_SCAN_PENDING`
            # (re-scan)" — and stripping those by hand is fiddly enough to get
            # silently wrong, which would make this whole test vacuous.
            match = re.search(r"\b[A-Z][A-Z_]{2,}\b", target_name)
            if match and match.group() in by_name:
                edges.add((source, by_name[match.group()]))
    return edges


def _implemented_edges(*, include_escape: bool = False) -> set[tuple[RunState, RunState]]:
    edges = set()
    for source, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            if not include_escape and target in ESCAPE_STATES:
                # Escape edges are a documented blanket rule, not per-row.
                if (source, target) not in _documented_edges():
                    continue
            edges.add((source, target))
    return edges


def test_the_document_and_the_code_describe_the_same_machine() -> None:
    documented = _documented_edges()
    implemented = _implemented_edges()

    missing = documented - implemented
    extra = implemented - documented

    assert not missing, f"documented in §8 but not implemented: {sorted(map(str, missing))}"
    assert not extra, f"implemented but not documented in §8: {sorted(map(str, extra))}"


def test_the_document_defines_a_meaningful_number_of_edges() -> None:
    """Guards the parser itself.

    If the §8 heading were renamed, the parser would silently find zero edges
    and the parity test above would pass vacuously.
    """
    assert len(_documented_edges()) > 40


def test_every_state_is_reachable_from_project_created() -> None:
    seen = {RunState.PROJECT_CREATED}
    frontier = [RunState.PROJECT_CREATED]

    while frontier:
        for target in ALLOWED_TRANSITIONS[frontier.pop()]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)

    unreachable = set(RunState) - seen
    assert not unreachable, f"unreachable states: {sorted(s.value for s in unreachable)}"


def test_no_state_is_a_dead_end() -> None:
    """Even terminal states return the project to monitoring."""
    dead_ends = [state.value for state, targets in ALLOWED_TRANSITIONS.items() if not targets]

    assert not dead_ends


def test_escape_states_are_reachable_from_every_non_terminal_state() -> None:
    for state in RunState:
        if state in TERMINAL_STATES:
            continue
        for escape in ESCAPE_STATES:
            assert can_transition(state, escape), f"{state.value} cannot reach {escape.value}"


def test_terminal_states_do_not_carry_escape_edges() -> None:
    """A finished run cannot 'fail' afterwards."""
    for state in TERMINAL_STATES:
        for escape in ESCAPE_STATES:
            if state is escape:
                continue
            assert not can_transition(state, escape)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunState.PROJECT_CREATED, RunState.VERIFIED),
        (RunState.MONITORING_ACTIVE, RunState.PR_CREATED),
        (RunState.VALIDATION_FAILED, RunState.SECURITY_REVIEW_PASSED),
        (RunState.APPROVAL_PENDING, RunState.PR_CREATED),
        (RunState.CHANGE_IRRELEVANT, RunState.MIGRATION_PENDING),
        (RunState.PR_CREATED, RunState.VERIFIED),
    ],
)
def test_illegal_transitions_are_rejected(source: RunState, target: RunState) -> None:
    assert not can_transition(source, target)


def test_a_run_cannot_skip_validation_to_reach_a_pull_request() -> None:
    """The property the whole machine exists to guarantee."""
    assert not can_transition(RunState.PATCH_READY, RunState.PR_PENDING)
    assert not can_transition(RunState.MIGRATION_RUNNING, RunState.PR_CREATING)
    assert not can_transition(RunState.VALIDATION_FAILED, RunState.FINAL_VALIDATION_PASSED)


def test_approval_cannot_be_skipped() -> None:
    assert not can_transition(RunState.APPROVAL_PENDING, RunState.FINAL_VALIDATION_RUNNING)
    assert can_transition(RunState.APPROVAL_PENDING, RunState.APPROVED)
    assert can_transition(RunState.APPROVED, RunState.FINAL_VALIDATION_RUNNING)


# --- Persistence ---------------------------------------------------------


async def _make_project_id(session: object) -> uuid.UUID:
    """A real project row; `state_transitions.project_id` is foreign-keyed."""
    from backend.models import Project, Repository, User

    user = User(google_subject=f"sub-{uuid.uuid4()}", email="sm@example.test")
    session.add(user)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]

    repository = Repository(owner="acme", name=f"repo-{uuid.uuid4().hex[:8]}")
    session.add(repository)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]

    project = Project(user_id=user.id, repository_id=repository.id, name="commerce-api")
    session.add(project)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    return project.id


async def test_a_legal_transition_is_persisted_with_its_evidence(database: None) -> None:
    async with session_scope() as session:
        project_id = await _make_project_id(session)

    async with session_scope() as session:
        record = await transition(
            session,
            from_state=RunState.VALIDATION_RUNNING,
            to_state=RunState.VALIDATION_PASSED,
            evidence=TransitionEvidence(
                reason="47/47 tests passed", actor="validator", detail={"passed": 47}
            ),
            project_id=project_id,
        )
        record_id = record.id

    async with session_scope() as session:
        stored = await session.get(StateTransition, record_id)

    assert stored is not None
    assert stored.from_state is RunState.VALIDATION_RUNNING
    assert stored.to_state is RunState.VALIDATION_PASSED
    assert stored.actor == "validator"
    assert stored.evidence["detail"]["passed"] == 47
    assert stored.occurred_at is not None


async def test_an_illegal_transition_raises_and_records_nothing(database: None) -> None:
    from sqlalchemy import func, select

    async with session_scope() as session:
        before = (
            await session.execute(select(func.count()).select_from(StateTransition))
        ).scalar_one()

    with pytest.raises(IllegalTransition) as excinfo:
        async with session_scope() as session:
            await transition(
                session,
                from_state=RunState.PROJECT_CREATED,
                to_state=RunState.PR_CREATED,
                evidence=TransitionEvidence(reason="should not happen", actor="system"),
            )

    assert excinfo.value.detail["from_state"] == "project_created"

    async with session_scope() as session:
        after = (
            await session.execute(select(func.count()).select_from(StateTransition))
        ).scalar_one()

    assert after == before


def test_transition_is_not_exposed_as_a_tool() -> None:
    """No agent may move a run directly."""
    from backend.agents.tools.registry import registry

    assert "transition" not in registry.names()
    assert not any("state" in name and "transition" in name for name in registry.names())
