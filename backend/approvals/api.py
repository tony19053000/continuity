"""C8-02: the human-control surface.

Approving is something only a person does, and this is the only door. Three
properties hold here and are tested as such:

* **The approver is the authenticated user.** `actor_user_id` comes from
  `current_user`, never from the request body. There is no field a caller can
  set to attribute a decision to someone else.
* **A decision is final.** Re-approving a rejected request would leave an audit
  trail showing only the outcome someone wanted.
* **Nothing under `backend/agents/` can reach this.** Enforced by an
  import-graph test rather than by care, because the rule has to survive code
  nobody has written yet.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import current_user, get_db_session
from backend.approvals.service import resolve
from backend.models import Approval, ApprovalStatus, Project, Severity, User
from backend.observability.logging import get_logger
from backend.shared.errors import NotFound, PermissionDenied
from backend.shared.redaction import redact_deep

logger = get_logger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


class ApprovalView(BaseModel):
    """What an approval looks like to the person deciding it.

    `requested_action` is secret-filtered on the way out: it is assembled from
    an agent's proposed action and may quote an argument.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    migration_run_id: uuid.UUID | None
    trigger: str
    risk: Severity
    status: ApprovalStatus
    requested_action: dict[str, object]
    agent_recommendation: str | None
    actor_user_id: uuid.UUID | None
    resolved_at: datetime | None

    @classmethod
    def of(cls, approval: Approval) -> ApprovalView:
        return cls(
            id=approval.id,
            project_id=approval.project_id,
            migration_run_id=approval.migration_run_id,
            trigger=approval.trigger,
            risk=approval.risk,
            status=approval.status,
            requested_action=redact_deep(dict(approval.requested_action or {})),
            agent_recommendation=approval.agent_recommendation,
            actor_user_id=approval.actor_user_id,
            resolved_at=approval.resolved_at,
        )


class Decision(BaseModel):
    """The body of a decision.

    Note what is absent: any way to name the approver. The decision is
    attributed to the authenticated session and to nothing else.

    `extra="forbid"` so a body carrying something like `actor_user_id` is
    refused rather than ignored. Ignoring it would be safe — nothing reads it —
    but it would let a caller believe they had attributed a decision to someone
    when they had not, and on this endpoint that misunderstanding matters.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


async def _owned_approval(
    session: AsyncSession, approval_id: uuid.UUID, user: User
) -> Approval:
    """Load an approval this user is entitled to decide.

    Ownership runs through the project. A user who can reach the id of someone
    else's approval still cannot act on it, and gets the same 404 as for one
    that does not exist — confirming existence would leak that it does.
    """
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise NotFound("That approval request does not exist.")

    project = await session.get(Project, approval.project_id)
    if project is None or project.user_id != user.id:
        logger.warning(
            "continuity.approval_access_refused",
            extra={"approval_id": str(approval_id), "user_id": str(user.id)},
        )
        raise NotFound("That approval request does not exist.")

    return approval


@router.get("", response_model=list[ApprovalView])
async def list_approvals(
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pending_only: bool = True,
) -> list[ApprovalView]:
    """Approvals on this user's own projects."""
    query = (
        select(Approval)
        .join(Project, Project.id == Approval.project_id)
        .where(Project.user_id == user.id)
        .order_by(Approval.created_at.desc())
    )
    if pending_only:
        query = query.where(Approval.status == ApprovalStatus.PENDING)

    rows = (await session.execute(query)).scalars().all()
    return [ApprovalView.of(row) for row in rows]


@router.get("/{approval_id}", response_model=ApprovalView)
async def get_approval(
    approval_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApprovalView:
    return ApprovalView.of(await _owned_approval(session, approval_id, user))


@router.post("/{approval_id}", response_model=ApprovalView, status_code=status.HTTP_200_OK)
async def decide(
    approval_id: uuid.UUID,
    body: Decision,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApprovalView:
    """Approve or reject. The only way an approval is ever written.

    `actor=user` is the whole security property: the approver is the
    authenticated session, and `resolve` takes a `User` object rather than an
    id so no caller can supply an arbitrary one.
    """
    approval = await _owned_approval(session, approval_id, user)

    resolved = await resolve(
        session, approval.id, actor=user, approved=body.decision == "approve"
    )

    logger.info(
        "continuity.approval_decided",
        extra={
            "approval_id": str(approval.id),
            "decision": resolved.status.value,
            "trigger": approval.trigger,
            "actor_user_id": str(user.id),
        },
    )
    return ApprovalView.of(resolved)


@router.post("/{approval_id}/revoke", response_model=ApprovalView)
async def revoke(
    approval_id: uuid.UUID,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApprovalView:
    """Withdraw an approval that has already been granted.

    The reason `require_granted` re-reads the row immediately before a protected
    action: a person can change their mind between granting and the action
    running, and the run must notice.
    """
    approval = await _owned_approval(session, approval_id, user)

    if approval.status is not ApprovalStatus.APPROVED:
        raise PermissionDenied(
            f"Only an APPROVED request can be revoked; this one is "
            f"{approval.status.value}."
        )

    approval.status = ApprovalStatus.REJECTED
    approval.actor_user_id = user.id
    await session.flush()

    logger.warning(
        "continuity.approval_revoked",
        extra={"approval_id": str(approval.id), "actor_user_id": str(user.id)},
    )
    return ApprovalView.of(approval)
