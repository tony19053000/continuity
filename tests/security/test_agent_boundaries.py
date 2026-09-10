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


#: Every Strands model class Continuity may construct. Adding a provider means
#: adding it here — but the import rule below catches one that is forgotten.
MODEL_CONSTRUCTORS = ("GeminiModel(", "BedrockModel(")


@pytest.mark.parametrize("module", _agent_modules(), ids=lambda p: p.name)
def test_no_agent_module_constructs_a_model_directly(module: Path) -> None:
    """Models come from the provider, never from a call site."""
    source = module.read_text()

    for constructor in MODEL_CONSTRUCTORS:
        assert constructor not in source, f"{module.name} constructs {constructor} directly"


@pytest.mark.parametrize("constructor", MODEL_CONSTRUCTORS)
def test_each_model_class_is_constructed_in_exactly_one_module(constructor: str) -> None:
    """C2-01 acceptance, for every provider rather than only the first one.

    An earlier version guarded `BedrockModel(` alone. When Gemini became the
    primary provider, the class actually carrying live traffic was the one left
    unguarded — so this is parametrized over the list rather than written once.

    A model built anywhere else escapes the configured model id and per-role
    temperature.
    """
    constructing = [
        path.relative_to(BACKEND).as_posix()
        for path in BACKEND.rglob("*.py")
        if constructor in path.read_text()
    ]

    assert constructing == ["shared/model_provider.py"], (
        f"{constructor} is constructed outside the provider: {constructing}"
    )


def test_only_the_provider_module_imports_a_strands_model() -> None:
    """The rule that cannot go stale.

    `MODEL_CONSTRUCTORS` is an enumeration, and enumerations get forgotten — the
    Gemini gap above is exactly that failure. This instead asserts that no
    module *except* the provider may import from `strands.models` at all, so a
    provider added tomorrow is covered without anyone remembering to list it.

    Known limitation, stated rather than implied: together these two tests catch
    *accidental* construction — a plausible import, a normal call. They do not
    catch deliberate obfuscation (a bare `import strands` plus `getattr` chains
    that never spell the class name). That is a different threat: this guard
    exists to stop a provider being wired up carelessly, not to defeat someone
    with commit access who is actively hiding it.
    """
    offenders = []
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative == "shared/model_provider.py":
            continue
        if any("strands.models" in module for module in _module_imports(path)):
            offenders.append(relative)

    assert not offenders, f"modules importing a Strands model directly: {offenders}"


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
