"""C8-06: the fallback for deployments that cannot receive webhooks.

A deployment behind a firewall, or one whose GitHub App has no webhook secret
configured, still needs to learn whether its pull request was merged. This polls
for the answer.

It converges on the same `settle()` the webhook uses, so the state machine does
not care which mechanism delivered the news — and there is exactly one place
where a merge outcome becomes a state change.

Polling asks GitHub directly rather than trusting anything inbound, so there is
no signature to verify: the answer comes from an authenticated outbound call on
an installation token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.github.webhooks import settle
from backend.models import PullRequest, Repository
from backend.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class PollResult:
    checked: int = 0
    settled: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def poll_open_pull_requests(
    session: AsyncSession, *, client: Any, repository_id: uuid.UUID | None = None
) -> PollResult:
    """Check every pull request Continuity still believes is open.

    `client` is anything exposing `get_pull_request(owner, name, number)`. Typed
    loosely on purpose: the polling job is also how a deployment with no
    outbound GitHub App would be stubbed out, and this module should not force
    one implementation.
    """
    result = PollResult()

    query = select(PullRequest).where(PullRequest.state == "open")
    if repository_id is not None:
        query = query.where(PullRequest.repository_id == repository_id)

    for record in (await session.execute(query)).scalars().all():
        repository = await session.get(Repository, record.repository_id)
        if repository is None:  # pragma: no cover - foreign key
            continue

        result.checked += 1
        try:
            payload = await client.get_pull_request(
                repository.owner, repository.name, record.number
            )
        except Exception as exc:
            # One unreachable repository must not stop the rest of the sweep.
            result.errors.append(f"#{record.number}: {type(exc).__name__}")
            logger.warning(
                "continuity.pr_poll_failed",
                extra={"number": record.number, "error": type(exc).__name__},
            )
            continue

        if str(payload.get("state") or "") != "closed":
            continue

        outcome = await settle(session, record, merged=bool(payload.get("merged")))
        result.settled.append(f"#{record.number}: {outcome}")

    logger.info(
        "continuity.pr_poll_complete",
        extra={
            "checked": result.checked,
            "settled": len(result.settled),
            "errors": len(result.errors),
        },
    )
    return result
