"""Tool registry and dispatcher.

This is the boundary between an agent and any real capability. Agents never hold
a reference to an underlying function; they hold a name, and every call goes:

    agent proposes  →  dispatcher  →  PolicyEngine.classify  →  ALLOW | ASK | DENY
                                          ↓ ALLOW     ↓ ASK          ↓ DENY
                                       execute    ApprovalRequest   refuse
                                                  + pause run       + audit

Two structural properties make that hold rather than merely describe it:

* A tool is registered with the `Action` it performs. There is no way to
  register one without declaring what it does, so an unclassified capability
  cannot appear.
* Each agent role carries an allowlist. A tool outside it is refused before the
  policy engine is even consulted, so a persuaded agent cannot reach a
  capability its role never had.

Every dispatch — including every refusal — writes a `ToolInvocation` row, so
"what did this system try to do, and what stopped it" is a query rather than a
guess.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AgentRole, PolicyDecision, ToolInvocation
from backend.models.enums import Severity
from backend.observability.logging import get_logger
from backend.security.policy import Action, ActionContext, PolicyResult, policy_engine
from backend.shared.errors import PolicyDenied
from backend.shared.redaction import redact

logger = get_logger(__name__)

ToolFunction = Callable[..., Awaitable[Any]]


class ToolNotRegistered(KeyError):
    """A name was dispatched that no tool claims."""


class ToolNotPermitted(PolicyDenied):
    """The tool exists, but this agent role is not allowed to call it."""

    code = "tool_not_permitted"


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    action: Action
    func: ToolFunction
    description: str


@dataclass(frozen=True, slots=True)
class ApprovalPause:
    """Returned when a dispatch needs a human.

    Deliberately a *value*, not an exception: an ASK is a normal, expected part
    of the workflow, and the coordinator pauses the run on it rather than
    treating it as a failure.
    """

    action: Action
    risk: Severity
    reason: str
    requested_action: dict[str, object]


class ToolRegistry:
    """Name → tool, plus the per-role allowlists."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._allowlists: dict[AgentRole, frozenset[str]] = {}

    def register(
        self, name: str, *, action: Action, description: str
    ) -> Callable[[ToolFunction], ToolFunction]:
        """Register a tool under `name`, declaring the action it performs."""

        def decorator(func: ToolFunction) -> ToolFunction:
            if name in self._tools:
                raise ValueError(f"Tool {name!r} is already registered.")
            self._tools[name] = RegisteredTool(
                name=name, action=action, func=func, description=description
            )
            return func

        return decorator

    def grant(self, role: AgentRole, tool_names: frozenset[str]) -> None:
        """Set the complete allowlist for a role, replacing any previous one."""
        unknown = tool_names - set(self._tools)
        if unknown:
            raise ValueError(f"Cannot grant unregistered tools to {role}: {sorted(unknown)}")
        self._allowlists[role] = tool_names

    def allowed_tools(self, role: AgentRole) -> frozenset[str]:
        """Tools this role may call. Absent role means none — never all."""
        return self._allowlists.get(role, frozenset())

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotRegistered(name) from exc

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)


#: Process-wide registry.
registry = ToolRegistry()


class ToolDispatcher:
    """The only way a tool is ever invoked."""

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self._registry = tool_registry or registry

    async def dispatch(
        self,
        *,
        session: AsyncSession,
        role: AgentRole,
        tool_name: str,
        arguments: dict[str, Any],
        context: ActionContext | None = None,
        agent_run_id: uuid.UUID | None = None,
    ) -> Any | ApprovalPause:
        """Classify, then execute, pause, or refuse.

        Returns the tool's result on ALLOW, an `ApprovalPause` on ASK, and
        raises `PolicyDenied` on DENY.
        """
        tool = self._registry.get(tool_name)

        # Role check first: a tool this role never had is refused before policy
        # is consulted, so an out-of-scope capability is not even evaluated.
        if tool_name not in self._registry.allowed_tools(role):
            await self._audit(
                session,
                agent_run_id=agent_run_id,
                tool_name=tool_name,
                arguments=arguments,
                decision=PolicyDecision.DENY,
                error=f"{role.value} is not permitted to call {tool_name}",
            )
            raise ToolNotPermitted(f"{role.value} may not call {tool_name}.")

        result: PolicyResult = policy_engine.classify(tool.action, context)

        if result.decision is PolicyDecision.DENY:
            await self._audit(
                session,
                agent_run_id=agent_run_id,
                tool_name=tool_name,
                arguments=arguments,
                decision=PolicyDecision.DENY,
                error=result.reason,
            )
            logger.warning(
                "continuity.unauthorized_action_attempt",
                extra={
                    "agent_role": role.value,
                    "tool": tool_name,
                    "action": result.action.value,
                    "reason": result.reason,
                },
            )
            raise PolicyDenied(result.reason)

        if result.decision is PolicyDecision.ASK:
            await self._audit(
                session,
                agent_run_id=agent_run_id,
                tool_name=tool_name,
                arguments=arguments,
                decision=PolicyDecision.ASK,
                error=None,
            )
            return ApprovalPause(
                action=result.action,
                risk=result.risk or Severity.HIGH,
                reason=result.reason,
                requested_action={"tool": tool_name, "arguments": _safe(arguments)},
            )

        started = time.perf_counter()
        try:
            value = await tool.func(**arguments)
        except Exception as exc:
            await self._audit(
                session,
                agent_run_id=agent_run_id,
                tool_name=tool_name,
                arguments=arguments,
                decision=PolicyDecision.ALLOW,
                error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
                succeeded=False,
            )
            raise

        await self._audit(
            session,
            agent_run_id=agent_run_id,
            tool_name=tool_name,
            arguments=arguments,
            decision=PolicyDecision.ALLOW,
            error=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            succeeded=True,
        )
        return value

    async def _audit(
        self,
        session: AsyncSession,
        *,
        agent_run_id: uuid.UUID | None,
        tool_name: str,
        arguments: dict[str, Any],
        decision: PolicyDecision,
        error: str | None,
        duration_ms: int | None = None,
        succeeded: bool | None = None,
    ) -> None:
        session.add(
            ToolInvocation(
                agent_run_id=agent_run_id,
                tool_name=tool_name,
                arguments=_safe(arguments),
                policy_decision=decision,
                succeeded=succeeded,
                error=redact(error) if error else None,
                duration_ms=duration_ms,
            )
        )
        await session.flush()


def _safe(arguments: dict[str, Any]) -> dict[str, Any]:
    """Secret-filter tool arguments before they are stored or returned.

    Arguments are attacker-influenced and end up in the activity feed, so they
    are filtered on the way in rather than trusted.
    """
    return {key: redact(value) if isinstance(value, str) else value for key, value in arguments.items()}


#: Process-wide dispatcher.
dispatcher = ToolDispatcher()
