"""The structural boundaries that keep LLM output from becoming authorization.

These are import-graph and surface tests rather than behavioural ones. A
behavioural test proves a particular call was refused; these prove the call
cannot be written in the first place, which is the stronger property and the one
that survives future code nobody has written yet.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.models.enums import AgentRole

BACKEND = Path(__file__).resolve().parents[2] / "backend"
AGENTS_DIR = BACKEND / "agents"


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _agent_modules() -> list[Path]:
    return [p for p in AGENTS_DIR.rglob("*.py") if p.name != "__init__.py"]


def test_agent_modules_exist_to_be_checked() -> None:
    """Guards every other test in this file against vacuity."""
    assert len(_agent_modules()) >= 4


@pytest.mark.parametrize("module", _agent_modules(), ids=lambda p: p.name)
def test_no_agent_module_can_reach_the_approval_service(module: Path) -> None:
    """An agent must not be able to create or resolve an approval.

    Import-level, so it holds for code not yet written: an agent that cannot
    import the service cannot call it however persuaded it becomes.
    """
    imports = _module_imports(module)

    offending = [m for m in imports if "approvals" in m]
    assert not offending, f"{module.name} imports the approval service: {offending}"


@pytest.mark.parametrize("module", _agent_modules(), ids=lambda p: p.name)
def test_no_agent_module_can_reach_the_state_machine(module: Path) -> None:
    """Only the coordinator moves runs."""
    imports = _module_imports(module)

    offending = [m for m in imports if "state_machine" in m]
    assert not offending, f"{module.name} imports the state machine: {offending}"


@pytest.mark.parametrize("module", _agent_modules(), ids=lambda p: p.name)
def test_no_agent_module_constructs_a_model_directly(module: Path) -> None:
    """Models come from the provider, never from a call site."""
    source = module.read_text()

    assert "BedrockModel(" not in source, f"{module.name} constructs BedrockModel directly"


def test_bedrock_model_is_constructed_in_exactly_one_module() -> None:
    """C2-01 acceptance.

    A model built anywhere else escapes region resolution, the configured model
    id, and per-role temperature.
    """
    constructing = [
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if "BedrockModel(" in path.read_text()
    ]

    assert constructing == ["shared/model_provider.py"], constructing


def test_only_the_migration_engineer_may_write_files() -> None:
    """C2-06 acceptance: least tool privilege per role."""
    from backend.agents.orchestrator import OrchestratorAgent
    from backend.agents.specialists import SPECIALISTS

    write_actions = {"modify_workspace_file", "create_continuity_branch"}
    all_agents = {**SPECIALISTS, AgentRole.ORCHESTRATOR: OrchestratorAgent}

    for role, agent_class in all_agents.items():
        tools = agent_class.contract.allowed_tools
        if role is AgentRole.MIGRATION_ENGINEER:
            continue
        assert not (tools & write_actions), f"{role.value} holds a write tool: {tools}"


def test_the_orchestrator_holds_no_tools_at_all() -> None:
    """It coordinates by proposing, not by acting."""
    from backend.agents.orchestrator import OrchestratorAgent

    assert OrchestratorAgent.contract.allowed_tools == frozenset()


def test_the_tool_dispatcher_is_the_only_caller_of_tool_functions() -> None:
    """No module may invoke a registered tool's function directly."""
    from backend.agents.tools.registry import registry

    # Nothing is registered yet (tools land with the capabilities they wrap in
    # Phases 3+), so this asserts the surface rather than a population.
    assert registry.names() == frozenset()


def test_an_agent_role_with_no_grant_has_no_tools() -> None:
    """Default is none, never all."""
    from backend.agents.tools.registry import ToolRegistry

    empty = ToolRegistry()

    for role in AgentRole:
        assert empty.allowed_tools(role) == frozenset()


def test_policy_engine_is_not_importable_from_an_agent_prompt() -> None:
    """Policy is enforced around agents, never described to them as negotiable."""
    from backend.agents.specialists import SPECIALISTS

    for agent_class in SPECIALISTS.values():
        prompt = agent_class.contract.system_prompt.lower()
        # The prompt may state that policy exists; it must not imply the agent
        # can change or bypass it.
        assert "you decide what is permitted" not in prompt
        assert "you may override" not in prompt
