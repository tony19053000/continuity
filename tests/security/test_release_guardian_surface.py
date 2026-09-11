"""C9-02 acceptance: **no code path performs a rollback.**

Asserted over the modules' public surface rather than by calling them, because
the property is "no such path exists" — a test that called a function and
observed nothing happening would prove only that one call did nothing.

The Guardian is the one part of Continuity with a reason to want a destructive
action: it is looking at a broken deployment and it knows which commit caused
it. That is exactly why the answer has to be a recommendation a person acts on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULES = [
    Path("backend/agents/release_guardian.py"),
    Path("backend/workers/post_merge_verify.py"),
    Path("backend/verification/environment.py"),
]

#: Names that would *do* something to a deployment or a repository's history.
#: Matched on attribute and function names, not on prose — a docstring
#: explaining that Continuity never reverts must not fail this test, and an
#: earlier version of this suite made exactly that mistake elsewhere.
FORBIDDEN_CALLS = {
    "revert",
    "rollback",
    "roll_back",
    "reset",
    "force_push",
    "delete_ref",
    "delete_branch",
    "redeploy",
    "deploy",
    "restart",
    "scale",
}


def _called_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_release_module_can_perform_a_rollback(module: Path) -> None:
    called = _called_names(module.read_text())
    offending = sorted(called & FORBIDDEN_CALLS)
    assert not offending, f"{module} calls {offending}"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_release_module_writes_to_a_repository(module: Path) -> None:
    """It reads a manifest and it makes synthetic requests. That is all.

    The GitHub client it is handed can create branches, blobs, and pull
    requests. Verification has no business using any of them.
    """
    called = _called_names(module.read_text())
    writes = {
        "create_branch",
        "create_blob",
        "create_tree",
        "create_commit",
        "update_branch",
        "create_pull_request",
        "write_file",
        "write_text",
    }
    assert not (called & writes), f"{module} writes to a repository"


def test_the_rollback_recommendation_reaches_no_branch() -> None:
    """A recommendation that something branched on would be an action.

    `rollback_recommended` is set from the agent's output and stored. Nothing
    reads it back to decide anything, and this asserts the absence rather than
    trusting the reading.
    """
    for module in MODULES:
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            condition = ast.unparse(node.test)
            # The one legitimate use is logging that a recommendation was made.
            if "rollback_recommended" not in condition:
                continue
            body = "\n".join(ast.unparse(item) for item in node.body)
            assert "logger." in body and len(node.body) == 1, (
                f"{module} branches on a rollback recommendation to do something "
                f"other than record it: {body}"
            )


def test_the_release_guardian_holds_no_tools() -> None:
    from backend.agents.specialists import ReleaseGuardianAgent

    assert ReleaseGuardianAgent.contract.allowed_tools == frozenset()


def test_verification_requests_carry_no_credential() -> None:
    """The manifest's URL is written in a repository, not by Continuity.

    Asserted structurally as well as behaviourally (see
    `tests/unit/verification/test_environment.py`), because the behavioural test
    only covers the headers the current code happens to set.
    """
    source = Path("backend/verification/environment.py").read_text()
    for forbidden in ("Authorization", "authorization", "token", "api_key", "Cookie"):
        assert f'"{forbidden}"' not in source, (
            f"{forbidden} appears as a key in the verification request path"
        )


def test_only_safe_methods_can_be_declared() -> None:
    """A manifest cannot ask Continuity to DELETE something to verify it."""
    from backend.verification.environment import _METHODS

    assert _METHODS == frozenset({"GET", "HEAD", "POST"})
