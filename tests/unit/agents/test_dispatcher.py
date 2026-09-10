"""C2-03 acceptance: every dispatch is classified, executed or refused, and audited."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from backend.agents.tools.registry import (
    ApprovalPause,
    ToolDispatcher,
    ToolNotPermitted,
    ToolNotRegistered,
    ToolRegistry,
)
from backend.models import AgentRole, PolicyDecision, ToolInvocation
from backend.models.session import session_scope
from backend.security.policy import Action, ActionContext
from backend.shared.errors import PolicyDenied
from tests.support.secret_samples import GITHUB_TOKEN


@pytest.fixture
def registry() -> ToolRegistry:
    """A registry with one tool per policy class."""
    reg = ToolRegistry()

    @reg.register("read_file", action=Action.READ_REPOSITORY_FILE, description="Read a file")
    async def read_file(path: str) -> str:
        return f"contents of {path}"

    @reg.register(
        "install_dependency", action=Action.INSTALL_DEPENDENCY, description="Add a package"
    )
    async def install_dependency(package: str) -> str:  # pragma: no cover - never runs
        return f"installed {package}"

    @reg.register("force_push", action=Action.FORCE_PUSH, description="Force push")
    async def force_push(branch: str) -> str:  # pragma: no cover - never runs
        return f"pushed {branch}"

    @reg.register("boom", action=Action.READ_REPOSITORY_FILE, description="Fails")
    async def boom() -> str:
        raise RuntimeError("tool exploded")

    reg.grant(
        AgentRole.IMPACT_ANALYST,
        frozenset({"read_file", "install_dependency", "force_push", "boom"}),
    )
    reg.grant(AgentRole.VALIDATOR, frozenset({"read_file"}))
    return reg


@pytest.fixture
def dispatcher(registry: ToolRegistry) -> ToolDispatcher:
    return ToolDispatcher(registry)


async def _invocations(session: Any) -> list[ToolInvocation]:
    return list((await session.execute(select(ToolInvocation))).scalars().all())


# --- ALLOW ---------------------------------------------------------------


async def test_an_allowed_tool_executes_and_is_audited(
    database: None, dispatcher: ToolDispatcher
) -> None:
    async with session_scope() as session:
        result = await dispatcher.dispatch(
            session=session,
            role=AgentRole.IMPACT_ANALYST,
            tool_name="read_file",
            arguments={"path": "payment_service.py"},
        )

    assert result == "contents of payment_service.py"

    async with session_scope() as session:
        (record,) = await _invocations(session)

    assert record.tool_name == "read_file"
    assert record.policy_decision is PolicyDecision.ALLOW
    assert record.succeeded is True
    assert record.duration_ms is not None


# --- ASK -----------------------------------------------------------------


async def test_an_elevated_tool_pauses_instead_of_executing(
    database: None, dispatcher: ToolDispatcher
) -> None:
    """ASK is a normal workflow outcome, returned as a value rather than raised."""
    async with session_scope() as session:
        result = await dispatcher.dispatch(
            session=session,
            role=AgentRole.IMPACT_ANALYST,
            tool_name="install_dependency",
            arguments={"package": "left-pad"},
        )

    assert isinstance(result, ApprovalPause)
    assert result.action is Action.INSTALL_DEPENDENCY
    assert result.requested_action["tool"] == "install_dependency"

    async with session_scope() as session:
        (record,) = await _invocations(session)

    assert record.policy_decision is PolicyDecision.ASK
    # The tool did not run, so there is no success to report.
    assert record.succeeded is None


# --- DENY ----------------------------------------------------------------


async def test_a_forbidden_tool_is_refused_and_audited(
    database: None, dispatcher: ToolDispatcher
) -> None:
    async with session_scope() as session:
        with pytest.raises(PolicyDenied):
            await dispatcher.dispatch(
                session=session,
                role=AgentRole.IMPACT_ANALYST,
                tool_name="force_push",
                arguments={"branch": "main"},
            )

    async with session_scope() as session:
        (record,) = await _invocations(session)

    assert record.policy_decision is PolicyDecision.DENY
    assert record.error is not None


async def test_context_can_deny_an_otherwise_allowed_tool(
    database: None, dispatcher: ToolDispatcher
) -> None:
    """Reading a file is allowed; reading outside the repository is not."""
    async with session_scope() as session:
        with pytest.raises(PolicyDenied):
            await dispatcher.dispatch(
                session=session,
                role=AgentRole.IMPACT_ANALYST,
                tool_name="read_file",
                arguments={"path": "../../.env"},
                context=ActionContext(target_path="../../.env", inside_repository=False),
            )


# --- Role allowlist ------------------------------------------------------


async def test_a_role_without_the_grant_is_refused_before_policy_runs(
    database: None, dispatcher: ToolDispatcher
) -> None:
    """A capability the role never had is not even evaluated."""
    async with session_scope() as session:
        with pytest.raises(ToolNotPermitted):
            await dispatcher.dispatch(
                session=session,
                role=AgentRole.VALIDATOR,
                tool_name="force_push",
                arguments={"branch": "main"},
            )

    async with session_scope() as session:
        (record,) = await _invocations(session)

    assert record.policy_decision is PolicyDecision.DENY
    assert "not permitted" in (record.error or "")


async def test_a_role_with_no_grant_at_all_can_call_nothing(
    database: None, dispatcher: ToolDispatcher
) -> None:
    async with session_scope() as session:
        with pytest.raises(ToolNotPermitted):
            await dispatcher.dispatch(
                session=session,
                role=AgentRole.SECURITY_REVIEWER,
                tool_name="read_file",
                arguments={"path": "x.py"},
            )


# --- Registry integrity --------------------------------------------------


async def test_an_unregistered_tool_cannot_be_dispatched(
    database: None, dispatcher: ToolDispatcher
) -> None:
    async with session_scope() as session:
        with pytest.raises(ToolNotRegistered):
            await dispatcher.dispatch(
                session=session,
                role=AgentRole.IMPACT_ANALYST,
                tool_name="definitely_not_a_tool",
                arguments={},
            )


def test_a_tool_cannot_be_registered_twice(registry: ToolRegistry) -> None:
    with pytest.raises(ValueError, match="already registered"):

        @registry.register("read_file", action=Action.READ_REPOSITORY_FILE, description="dup")
        async def duplicate() -> None: ...


def test_granting_an_unregistered_tool_fails_loudly(registry: ToolRegistry) -> None:
    """A typo in an allowlist must not silently grant nothing."""
    with pytest.raises(ValueError, match="unregistered"):
        registry.grant(AgentRole.VALIDATOR, frozenset({"no_such_tool"}))


# --- Failure and secrets -------------------------------------------------


async def test_a_failing_tool_is_audited_as_failed(
    database: None, dispatcher: ToolDispatcher
) -> None:
    async with session_scope() as session:
        with pytest.raises(RuntimeError):
            await dispatcher.dispatch(
                session=session, role=AgentRole.IMPACT_ANALYST, tool_name="boom", arguments={}
            )

    async with session_scope() as session:
        (record,) = await _invocations(session)

    assert record.succeeded is False
    assert "exploded" in (record.error or "")


async def test_tool_arguments_are_secret_filtered_before_storage(
    database: None, dispatcher: ToolDispatcher
) -> None:
    """Arguments reach the activity feed, so they are filtered on the way in."""
    async with session_scope() as session:
        await dispatcher.dispatch(
            session=session,
            role=AgentRole.IMPACT_ANALYST,
            tool_name="read_file",
            arguments={"path": f"token={GITHUB_TOKEN}"},
        )

    async with session_scope() as session:
        (record,) = await _invocations(session)

    assert GITHUB_TOKEN not in str(record.arguments)
    assert "[REDACTED]" in str(record.arguments)
