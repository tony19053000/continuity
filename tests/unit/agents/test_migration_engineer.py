"""C7-02: the rules a proposed patch has to survive.

The engineer is scripted. What is under test is everything around it — scope,
secrets, tests, dependencies — and none of that is model behaviour. Each test
puts a specific hostile or careless patch in front of the rules and asserts what
reaches disk.

The workspace is real, so "reaches disk" means exactly that.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.agents import migration_engineer as engineer_module
from backend.agents.migration_engineer import (
    count_tests,
    declared_dependencies,
    is_test_path,
    produce_patch,
)
from backend.migrations.workspace import WorkspaceManager
from backend.models.enums import FindingCategory, PolicyDecision
from backend.observability.execution_audit import NullExecutionAudit
from tests.support.migration_fixtures import (
    CLIENT_V2,
    IMPACT_SET,
    ScriptedEngineer,
    StubProvider,
    change,
    edit,
    fix_it,
    impact,
    output,
    write_fixture_repository,
)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "userrepo"
    repo.mkdir()
    write_fixture_repository(repo)

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607 - fixture setup
            cwd=repo,
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )

    run("init", "-b", "main")
    run("config", "user.email", "t@example.test")
    run("config", "user.name", "Test")
    run("add", "-A")
    run("commit", "-m", "initial")
    return repo


@pytest.fixture
def manager(source_repo: Path, tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(
        source_repo, workspace_root=tmp_path / "ws", audit=NullExecutionAudit()
    )


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    def install(*outputs: Any) -> ScriptedEngineer:
        runner = ScriptedEngineer(*outputs)
        original = engineer_module.MigrationEngineerAgent

        class Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, provider: Any, **kwargs: Any) -> None:
                super().__init__(provider, runner=runner, **kwargs)

        monkeypatch.setattr(engineer_module, "MigrationEngineerAgent", Patched)
        return runner

    return install


async def _patch(manager: WorkspaceManager, **kwargs: Any):
    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        result = await produce_patch(
            ws,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            change=change(),
            impact=impact(),
            impact_set=IMPACT_SET,
            model_provider=StubProvider(),
            **kwargs,
        )
        return result, ws.read_file("app/client.py"), ws


# --- the happy path ------------------------------------------------------


async def test_a_patch_inside_the_impact_set_is_applied(
    manager: WorkspaceManager, scripted: Any
) -> None:
    scripted(fix_it())

    patch, client, _ = await _patch(manager)

    assert patch.applied == ["app/client.py"]
    assert patch.rejected == []
    assert client == CLIENT_V2
    assert "currency" in patch.diff
    assert patch.blocking_findings == []


async def test_the_engineer_sees_the_impact_set_and_its_callers(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Bounded retrieval, never a repository dump (`CLAUDE.md` §3.7).

    The impact set is editable; the tests covering it are read-only context.
    Both halves matter: without the callers the engineer is asked to keep a
    signature compatible with code it cannot see, and live Gemini duly wrote a
    patch making `currency` a required positional argument — correct in
    isolation, and it broke every existing call.
    """
    runner = scripted(fix_it())

    await _patch(manager)

    assert runner.calls == 1
    prompt = runner.prompts[0]
    assert "Files you may edit:" in prompt
    assert "--- app/client.py ---" in prompt
    assert "DO NOT EDIT" in prompt
    assert "tests/test_client.py (read-only)" in prompt
    assert "def test_charge_sends_every_required_field" in prompt
    # Not a dump: a file in neither the impact set nor the covering tests.
    assert "app/unrelated.py" not in prompt


# --- scope ---------------------------------------------------------------

async def test_an_edit_outside_the_impact_set_is_discarded(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """C7-02 acceptance, checked by comparing the diff against the impact set.

    `justification` is required on every edit, so it cannot be what marks an
    out-of-scope change — the engineer has to say specifically that this file is
    outside the set and why. Without that, the edit never reaches disk.
    """
    scripted(
        output(
            edit("app/client.py", CLIENT_V2),
            edit("app/unrelated.py", "VALUE = 999\n"),
        )
    )

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        patch = await produce_patch(
            ws,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            change=change(),
            impact=impact(),
            impact_set=IMPACT_SET,
            model_provider=StubProvider(),
        )
        unrelated = ws.read_file("app/unrelated.py")
        changed = await ws.changed_files()

    assert patch.applied == ["app/client.py"]
    assert [r.path for r in patch.rejected] == ["app/unrelated.py"]
    assert "impact set" in patch.rejected[0].reason
    assert unrelated == "VALUE = 1\n"
    # The measurable form of the criterion: the diff's file list is the set.
    assert changed == ["app/client.py"]


async def test_an_out_of_scope_edit_with_a_justification_is_applied(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Sometimes another file genuinely must change. Saying so is the price."""
    scripted(
        output(
            edit("app/client.py", CLIENT_V2),
            edit(
                "app/unrelated.py",
                "VALUE = 999\n",
                out_of_impact_justification=(
                    "client.py imports VALUE and the new contract changes its meaning"
                ),
            ),
        )
    )

    patch, _, _ = await _patch(manager)

    assert sorted(patch.applied) == ["app/client.py", "app/unrelated.py"]
    assert patch.rejected == []


async def test_an_out_of_scope_edit_produces_a_finding_when_undeclared(
    manager: WorkspaceManager, scripted: Any
) -> None:
    scripted(output(edit("app/unrelated.py", "VALUE = 999\n")))

    patch, _, _ = await _patch(manager)

    categories = [finding.category for finding in patch.findings]
    assert FindingCategory.TOOL_MISUSE in categories


@pytest.mark.parametrize(
    "path", ["../escaped.py", "/etc/passwd", "app/../../escaped.py"]
)
async def test_an_edit_outside_the_workspace_never_lands(
    path: str, manager: WorkspaceManager, scripted: Any
) -> None:
    """C7-02 acceptance: only files inside the workspace are modified.

    These carry a justification, so scope lets them through — and the workspace
    refuses them anyway. Two independent controls, which is the point.
    """
    scripted(
        output(edit(path, "malicious\n", out_of_impact_justification="claims to be needed"))
    )

    patch, _, _ = await _patch(manager)

    assert patch.applied == []
    assert [r.path for r in patch.rejected] == [path]
    assert any(f.policy_decision is PolicyDecision.DENY for f in patch.findings)


# --- secrets -------------------------------------------------------------


async def test_a_patch_containing_a_credential_is_discarded(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """C7-02 acceptance. Refused before it reaches disk, not after.

    A secret written into a workspace is already a secret a later command could
    echo into a log or a pull request body.
    """
    from tests.support.secret_samples import GITHUB_TOKEN

    scripted(output(edit("app/client.py", f'TOKEN = "{GITHUB_TOKEN}"\n' + CLIENT_V2)))

    patch, client, _ = await _patch(manager)

    assert patch.applied == []
    assert GITHUB_TOKEN not in client
    (finding,) = [f for f in patch.findings if f.category is FindingCategory.SECRET_EXPOSURE]
    assert finding.policy_decision is PolicyDecision.DENY
    assert finding.blocking
    # The finding names the kind, never the value.
    assert GITHUB_TOKEN not in finding.summary
    assert GITHUB_TOKEN not in str(patch.summary())


@pytest.mark.parametrize(
    "sample",
    ["AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "STRIPE_LIVE_KEY", "PRIVATE_KEY_BLOCK"],
)
async def test_every_credential_shape_is_caught(
    sample: str, manager: WorkspaceManager, scripted: Any
) -> None:
    import tests.support.secret_samples as samples

    secret = getattr(samples, sample)
    scripted(output(edit("app/client.py", f'X = """{secret}"""\n')))

    patch, _, _ = await _patch(manager)

    assert patch.applied == []
    assert any(f.category is FindingCategory.SECRET_EXPOSURE for f in patch.findings)


# --- tests ---------------------------------------------------------------


def test_the_contract_has_no_way_to_delete_a_file() -> None:
    """C7-02 acceptance: no test file is deleted.

    Enforced by the shape of `FileEdit` rather than by a check — there is no
    delete operation to guard, so there is no guard to forget.
    """
    from backend.agents.contracts import FileEdit

    assert set(FileEdit.model_fields) == {
        "path",
        "new_content",
        "justification",
        "out_of_impact_justification",
    }


async def test_a_patch_that_removes_tests_is_discarded(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Emptying a test file is deleting it by another name.

    `02_ARCHITECTURE.md` §12: a failing test may not be weakened to reach PASS.
    """
    scripted(
        output(
            edit(
                "tests/test_client.py",
                "def test_charge_returns_the_amount():\n    assert True\n",
                out_of_impact_justification="the test is wrong",
            ),
            modifies_tests=True,
            test_modification_justification="the first test is invalid",
        )
    )

    async with manager.open(run_id=uuid.uuid4(), target_branch="continuity/x") as ws:
        before = ws.read_file("tests/test_client.py")
        patch = await produce_patch(
            ws,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            change=change(),
            impact=impact(),
            impact_set=IMPACT_SET,
            model_provider=StubProvider(),
        )
        after = ws.read_file("tests/test_client.py")

    assert patch.applied == []
    assert after == before
    weakened = [f for f in patch.findings if f.category is FindingCategory.TEST_WEAKENED]
    assert weakened
    assert all(f.policy_decision is PolicyDecision.ASK for f in weakened)


async def test_an_undeclared_test_modification_is_a_finding(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """The shape of a test being quietly adjusted until it passes."""
    scripted(
        output(
            edit(
                "tests/test_client.py",
                "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
                out_of_impact_justification="needed",
            )
        )
    )

    patch, _, _ = await _patch(manager)

    (finding,) = [f for f in patch.findings if f.category is FindingCategory.TEST_WEAKENED]
    assert "without declaring it" in finding.summary
    assert finding.policy_decision is PolicyDecision.ASK
    assert patch.modifies_tests
    assert not patch.declared_test_modification


async def test_a_declared_test_modification_is_still_a_finding(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Declaring it makes it a human decision, not an approved one."""
    scripted(
        output(
            edit(
                "tests/test_client.py",
                "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
                out_of_impact_justification="needed",
            ),
            modifies_tests=True,
            test_modification_justification="the assertion tested the old contract",
        )
    )

    patch, _, _ = await _patch(manager)

    (finding,) = [f for f in patch.findings if f.category is FindingCategory.TEST_WEAKENED]
    assert "declared by the engineer" in finding.summary
    assert finding.policy_decision is PolicyDecision.ASK


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_client.py", True),
        ("test/test_x.py", True),
        ("app/test_helpers.py", True),
        ("src/__tests__/app.js", True),
        ("src/app.test.ts", True),
        ("src/app.spec.tsx", True),
        ("pkg/thing_test.go", True),
        ("app/client.py", False),
        ("app/latest.py", False),
        ("app/contest.py", False),
    ],
)
def test_test_paths_are_recognised(path: str, expected: bool) -> None:
    """A false negative lets a test be rewritten silently, so this is broad.

    `latest.py` and `contest.py` are here because they contain "test" and are
    not tests — a substring check would catch them.
    """
    assert is_test_path(path) is expected


@pytest.mark.parametrize(
    ("content", "count"),
    [
        ("def test_a(): pass\ndef test_b(): pass\n", 2),
        ("it('works', () => {})\n", 1),
        ("describe('x', () => { it('a', () => {}) })\n", 2),
        ("func TestThing(t *testing.T) {}\n", 1),
        ("def helper(): pass\n", 0),
    ],
)
def test_tests_are_counted_across_ecosystems(content: str, count: int) -> None:
    assert count_tests(content) == count


# --- dependencies --------------------------------------------------------


async def test_a_new_dependency_produces_an_ask_finding(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """C7-02 acceptance: `new_dependency` → ASK.

    The supply chain is part of the review surface
    (`03_SECURITY_ACCESS.md` §9).
    """
    scripted(
        output(
            edit("app/client.py", CLIENT_V2),
            edit(
                "pyproject.toml",
                '[project]\nname = "fixture"\nversion = "0"\n'
                'dependencies = ["httpx", "left-pad"]\n'
                "\n[tool.pytest.ini_options]\naddopts = \"-q\"\n",
                out_of_impact_justification="the new client needs it",
            ),
        )
    )

    patch, _, _ = await _patch(manager)

    assert patch.new_dependencies == ["left-pad"]
    (finding,) = [f for f in patch.findings if f.category is FindingCategory.NEW_DEPENDENCY]
    assert finding.policy_decision is PolicyDecision.ASK
    assert finding.blocking


async def test_a_dependency_is_detected_even_when_the_agent_does_not_mention_it(
    manager: WorkspaceManager, scripted: Any
) -> None:
    """Detection parses the manifest; it does not ask.

    An omission in the agent's own list would otherwise walk a package straight
    past the review.
    """
    scripted(
        output(
            edit(
                "pyproject.toml",
                '[project]\nname = "fixture"\nversion = "0"\n'
                'dependencies = ["httpx", "requests"]\n',
                out_of_impact_justification="needed",
            ),
            new_dependencies=[],
        )
    )

    patch, _, _ = await _patch(manager)

    assert patch.new_dependencies == ["requests"]


async def test_an_unchanged_manifest_produces_no_dependency_finding(
    manager: WorkspaceManager, scripted: Any
) -> None:
    scripted(fix_it())

    patch, _, _ = await _patch(manager)

    assert patch.new_dependencies == []
    assert not [f for f in patch.findings if f.category is FindingCategory.NEW_DEPENDENCY]


@pytest.mark.parametrize(
    ("manifest", "content", "expected"),
    [
        ("pyproject.toml", '[project]\ndependencies = ["httpx>=0.27", "PyYAML"]\n', {"httpx", "pyyaml"}),
        ("requirements.txt", "httpx==0.27.0\n# comment\nrich\n", {"httpx", "rich"}),
        ("package.json", '{"dependencies": {"react": "^19"}, "devDependencies": {"vitest": "^3"}}', {"react", "vitest"}),
        # A manifest that will not parse yields nothing, rather than a phantom
        # dependency list assembled from parse damage.
        ("pyproject.toml", "[project\nbroken", set()),
        ("package.json", "{not json", set()),
    ],
    ids=["pyproject", "requirements", "package-json", "broken-toml", "broken-json"],
)
def test_dependencies_are_parsed_not_pattern_matched(
    manifest: str, content: str, expected: set[str]
) -> None:
    assert declared_dependencies(manifest, content) == expected
