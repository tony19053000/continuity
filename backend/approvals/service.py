"""Approval state.

Approval is deterministic persisted state, never a model output. This module is
the only way to create or resolve one, and it is deliberately unreachable from
`backend/agents/` — an import-graph test in `tests/security/` fails if any agent
module can reach it, directly or transitively.

Two properties matter and are both enforced here and in the schema:

* Only an authenticated `User` can resolve a request. The signature requires a
  `User` instance, and `backend/api/deps.py` is the only place one is produced
  from a request.
* A resolved approval is final. Re-resolving raises rather than silently
  overwriting, so an approval cannot be flipped after the fact.

`03_SECURITY_ACCESS.md` §4. C8-02 adds the HTTP surface and the resume flow on
top of this.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Approval, ApprovalStatus, Severity, User
from backend.shared.errors import ApprovalRequired, ContinuityError


class ApprovalAlreadyResolved(ContinuityError):
    """A resolved approval cannot be changed.

    Without this, a rejected request could be quietly re-approved and the audit
    trail would show only the final state.
    """

    code = "approval_already_resolved"
    status_code = 409
    message = "This approval has already been decided."


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What an agent is asking permission to do.

    Constructed by the tool dispatcher from a policy ASK decision. It carries no
    authority of its own — it is a question, and only a human answers it.
    """

    project_id: uuid.UUID
    trigger: str
    risk: Severity
    requested_action: dict[str, object]
    migration_run_id: uuid.UUID | None = None
    agent_recommendation: str | None = None


async def create_request(session: AsyncSession, request: ApprovalRequest) -> Approval:
    """Record a pending approval and return it."""
    approval = Approval(
        project_id=request.project_id,
        migration_run_id=request.migration_run_id,
        trigger=request.trigger,
        risk=request.risk,
        requested_action=dict(request.requested_action),
        agent_recommendation=request.agent_recommendation,
        status=ApprovalStatus.PENDING,
    )
    session.add(approval)
    await session.flush()
    return approval


async def resolve(
    session: AsyncSession,
    approval_id: uuid.UUID,
    *,
    actor: User,
    approved: bool,
) -> Approval:
    """Record a human decision.

    `actor` is a `User`, not an id, so a caller cannot pass an arbitrary UUID —
    the only way to obtain one for a request is `backend.api.deps.current_user`.
    """
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise ApprovalRequired("That approval request does not exist.")

    if approval.status is not ApprovalStatus.PENDING:
        raise ApprovalAlreadyResolved(
            f"Approval {approval_id} is already {approval.status.value}."
        )

    approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    approval.actor_user_id = actor.id
    approval.resolved_at = datetime.now(UTC)
    await session.flush()
    return approval


async def is_approved(session: AsyncSession, approval_id: uuid.UUID) -> bool:
    """Whether this approval is currently APPROVED.

    Callers re-check immediately before performing the protected action rather
    than trusting a decision made earlier in the run — an approval can be
    rejected between the grant and the execution.
    """
    status = (
        await session.execute(select(Approval.status).where(Approval.id == approval_id))
    ).scalar_one_or_none()
    return status is ApprovalStatus.APPROVED


async def pending_for_run(
    session: AsyncSession, migration_run_id: uuid.UUID
) -> list[Approval]:
    """Every unresolved request blocking a run."""
    result = await session.execute(
        select(Approval).where(
            Approval.migration_run_id == migration_run_id,
            Approval.status == ApprovalStatus.PENDING,
        )
    )
    return list(result.scalars().all())
