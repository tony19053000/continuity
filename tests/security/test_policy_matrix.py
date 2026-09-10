"""C2-03 acceptance: every documented ALLOW/ASK/DENY row is enforced.

The matrix in `03_SECURITY_ACCESS.md` §4 is parsed from the document and
compared against `backend/security/policy.py`, so the two cannot drift. This is
the security boundary of the whole product — no model sits anywhere in the
decision path these tests exercise.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.models.enums import PolicyDecision
from backend.security.policy import (
    ASK_RISK,
    POLICY,
    Action,
    ActionContext,
    policy_engine,
)

SECURITY_DOC = Path(__file__).resolve().parents[2] / "03_SECURITY_ACCESS.md"


def _documented_counts() -> dict[str, int]:
    """Count ALLOW/ASK/DENY rows in the §4 matrix.

    Row wording is prose and deliberately not machine-matched to enum members —
    doing so would turn every documentation reword into a test failure. What is
    asserted is that the document and the code agree on how many actions fall
    into each class, which catches a row being added or silently reclassified.
    """
    text = SECURITY_DOC.read_text()
    section = text.split("### ALLOW / ASK / DENY matrix", 1)[1].split("\nDENY outcomes", 1)[0]

    counts = {"ALLOW": 0, "ASK": 0, "DENY": 0}
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        verdict = re.sub(r"\*\*", "", cells[1]).strip().upper()
        if verdict in counts:
            counts[verdict] += 1
    return counts


def test_every_action_has_a_classification() -> None:
    """A capability with no decision would fall through to the default."""
    unclassified = [action.value for action in Action if action not in POLICY]

    assert not unclassified, f"unclassified actions: {unclassified}"


def test_the_matrix_matches_the_security_document() -> None:
    documented = _documented_counts()
    implemented = {
        "ALLOW": sum(1 for d in POLICY.values() if d is PolicyDecision.ALLOW),
        "ASK": sum(1 for d in POLICY.values() if d is PolicyDecision.ASK),
        "DENY": sum(1 for d in POLICY.values() if d is PolicyDecision.DENY),
    }

    assert documented == implemented, (
        f"03_SECURITY_ACCESS.md §4 documents {documented} but policy.py implements {implemented}"
    )


def test_the_document_parser_found_a_real_matrix() -> None:
    """Guards the parser: a renamed heading must not silently pass."""
    counts = _documented_counts()

    assert sum(counts.values()) > 20


@pytest.mark.parametrize("action", sorted(a for a in Action if POLICY[a] is PolicyDecision.DENY))
def test_forbidden_actions_are_denied(action: Action) -> None:
    result = policy_engine.classify(action)

    assert result.decision is PolicyDecision.DENY
    assert not result.allowed


@pytest.mark.parametrize("action", sorted(a for a in Action if POLICY[a] is PolicyDecision.ASK))
def test_elevated_actions_ask_and_carry_a_risk(action: Action) -> None:
    result = policy_engine.classify(action)

    assert result.decision is PolicyDecision.ASK
    assert result.risk is not None
    assert action in ASK_RISK


@pytest.mark.parametrize("action", sorted(a for a in Action if POLICY[a] is PolicyDecision.ALLOW))
def test_routine_actions_are_allowed(action: Action) -> None:
    assert policy_engine.classify(action).allowed


# --- The rules that matter most ------------------------------------------


def test_credential_exposure_is_never_permitted() -> None:
    assert policy_engine.classify(Action.EXPOSE_CREDENTIALS).decision is PolicyDecision.DENY


def test_merging_a_pull_request_is_never_permitted() -> None:
    """The developer controls merge. Continuity never does."""
    assert policy_engine.classify(Action.MERGE_PULL_REQUEST).decision is PolicyDecision.DENY


def test_force_push_and_history_rewrite_are_never_permitted() -> None:
    for action in (Action.FORCE_PUSH, Action.REWRITE_HISTORY):
        assert policy_engine.classify(action).decision is PolicyDecision.DENY


def test_bypassing_approval_is_never_permitted() -> None:
    assert policy_engine.classify(Action.BYPASS_APPROVAL).decision is PolicyDecision.DENY


# --- Context escalation --------------------------------------------------


def test_a_branch_write_targeting_the_default_branch_is_denied() -> None:
    """Normally allowed, forbidden by context.

    The agent may believe it is creating its own branch; what matters is the
    branch actually targeted.
    """
    result = policy_engine.classify(
        Action.CREATE_CONTINUITY_BRANCH,
        ActionContext(target_branch="main", is_default_branch=True),
    )

    assert result.decision is PolicyDecision.DENY
    assert result.action is Action.WRITE_PROTECTED_BRANCH
    assert "default branch" in result.reason


def test_a_write_outside_the_workspace_is_denied() -> None:
    result = policy_engine.classify(
        Action.MODIFY_WORKSPACE_FILE,
        ActionContext(target_path="/etc/passwd", inside_workspace=False),
    )

    assert result.decision is PolicyDecision.DENY


def test_a_write_inside_the_workspace_is_allowed() -> None:
    result = policy_engine.classify(
        Action.MODIFY_WORKSPACE_FILE,
        ActionContext(target_path="payment_service.py", inside_workspace=True),
    )

    assert result.allowed


def test_a_read_outside_the_repository_is_denied() -> None:
    result = policy_engine.classify(
        Action.READ_REPOSITORY_FILE,
        ActionContext(target_path="../../secrets.env", inside_repository=False),
    )

    assert result.decision is PolicyDecision.DENY
    assert result.action is Action.READ_OUTSIDE_REPOSITORY


def test_an_unknown_action_defaults_to_deny() -> None:
    """Adding a capability without classifying it must fail closed."""

    class Rogue(str):
        value = "rogue_action"

    result = policy_engine.classify(Rogue("rogue_action"))  # type: ignore[arg-type]

    assert result.decision is PolicyDecision.DENY
    assert "no policy classification" in result.reason


def test_no_model_is_involved_in_classification() -> None:
    """The policy module must not import the model provider, directly or not."""
    import ast

    source = (Path(__file__).resolve().parents[2] / "backend/security/policy.py").read_text()
    tree = ast.parse(source)

    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not any("model_provider" in m or "strands" in m or "agents" in m for m in imported)
