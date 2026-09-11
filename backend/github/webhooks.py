"""C8-06: learning a pull request's outcome, without assuming it.

`MERGE_WAITING` advances only when Continuity finds out what happened. This is
the first of the two ways it can find out (`02_ARCHITECTURE.md` §15).

Three properties, in the order they matter:

* **A delivery is authenticated or it is nothing.** HMAC-SHA256 over the raw
  body, compared with `hmac.compare_digest`. An unsigned or mis-signed delivery
  is a 401 and an audit row — never a state change.
* **An authenticated delivery is still untrusted input.** GitHub says a pull
  request closed; Continuity uses that only to *look up its own record* by
  repository and number. Nothing in the payload becomes state, and no field in
  it can trigger an action that would otherwise need approval.
* **No secret configured means no webhook endpoint working.** The handler
  refuses every delivery rather than accepting unsigned ones, and the run stays
  in `MERGE_WAITING`. A deployment that cannot verify deliveries does not get to
  act on them.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_settings_dep
from backend.models import AuditEvent, MigrationRun, Project, PullRequest, Repository
from backend.models.enums import ActivityEventKind, RunState
from backend.models.schemas import TransitionEvidence
from backend.models.session import session_scope
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.shared.config import GitHubAppConfig, Settings
from backend.shared.errors import AuthenticationRequired

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks/github", tags=["webhooks"])

SIGNATURE_HEADER: Final = "X-Hub-Signature-256"
EVENT_HEADER: Final = "X-GitHub-Event"
DELIVERY_HEADER: Final = "X-GitHub-Delivery"

#: Bodies larger than this are refused before hashing. A webhook endpoint is
#: unauthenticated until the signature is checked, so it must not be a way to
#: make Continuity allocate arbitrary memory.
MAX_BODY_BYTES: Final = 2_000_000


def expected_signature(secret: str, body: bytes) -> str:
    """The `sha256=...` value GitHub sends for this body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def signature_matches(secret: str, body: bytes, provided: str | None) -> bool:
    """Constant-time comparison.

    `==` on a digest leaks how much of the prefix was right, one comparison at a
    time. `compare_digest` is the whole reason this is a function rather than an
    inline check.
    """
    if not provided:
        return False
    return hmac.compare_digest(expected_signature(secret, body), provided)


async def _audit(kind: str, detail: dict[str, Any]) -> None:
    """Record a refusal, in its own transaction.

    Deliberately **not** the request's session. Every caller of this raises
    immediately afterwards, and the request session rolls back on the way out —
    so an audit row written there would be discarded with it, and a refused
    delivery would leave no trace at all. Auditing refusals is most of the point
    of auditing here, so the row commits on its own.
    """
    async with session_scope() as session:
        session.add(
            AuditEvent(
                kind=kind,
                actor="github_webhook",
                project_id=None,
                detail=detail,
                occurred_at=datetime.now(UTC),
            )
        )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    x_github_event: Annotated[str | None, Header(alias=EVENT_HEADER)] = None,
    x_hub_signature_256: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
    x_github_delivery: Annotated[str | None, Header(alias=DELIVERY_HEADER)] = None,
) -> dict[str, object]:
    """Receive a GitHub App delivery."""
    config = settings.github_app
    secret = (
        config.webhook_secret.get_secret_value()
        if isinstance(config, GitHubAppConfig) and config.webhook_secret
        else ""
    )

    if not secret:
        # Webhooks are not configured. Refusing is the honest answer: accepting
        # unsigned deliveries would let anyone who can reach this URL advance a
        # migration run.
        await _audit(
            "webhook.refused",
            {"reason": "no webhook secret configured", "delivery": x_github_delivery},
        )
        raise AuthenticationRequired(
            "Webhook deliveries are not accepted: no signing secret is configured."
        )

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        await _audit("webhook.refused", {"reason": "body too large", "bytes": len(body)})
        raise AuthenticationRequired("Delivery refused.")

    if not signature_matches(secret, body, x_hub_signature_256):
        await _audit(
            "webhook.refused",
            {
                "reason": "signature mismatch",
                "delivery": x_github_delivery,
                "event": x_github_event,
                # The provided signature is not recorded: it is attacker-supplied
                # and recording it would put chosen bytes in the audit table.
            },
        )
        logger.warning(
            "continuity.webhook_signature_rejected",
            extra={"event": x_github_event, "delivery": x_github_delivery},
        )
        raise AuthenticationRequired("Delivery signature could not be verified.")

    if x_github_event != "pull_request":
        # Verified, and not something this receiver acts on.
        return {"status": "ignored", "event": x_github_event}

    payload = await request.json()
    outcome = await apply_pull_request_event(session, payload)
    return {"status": outcome}


async def apply_pull_request_event(
    session: AsyncSession, payload: dict[str, Any]
) -> str:
    """Advance a run from a `pull_request` payload.

    The payload is a *pointer*, not state. Its repository full name and PR
    number are used to find Continuity's own row; everything acted upon comes
    from that row and from the state machine.
    """
    pull = payload.get("pull_request") or {}
    repository_name = ((payload.get("repository") or {}).get("full_name")) or ""
    number = pull.get("number")

    if not isinstance(number, int) or "/" not in repository_name:
        return "ignored"

    owner, _, name = repository_name.partition("/")
    repository = (
        await session.execute(
            select(Repository).where(Repository.owner == owner, Repository.name == name)
        )
    ).scalar_one_or_none()

    if repository is None:
        # A delivery for a repository Continuity does not track. Verified, and
        # still none of our business.
        return "unknown_repository"

    record = (
        await session.execute(
            select(PullRequest).where(
                PullRequest.repository_id == repository.id,
                PullRequest.number == number,
            )
        )
    ).scalar_one_or_none()

    if record is None:
        return "unknown_pull_request"

    merged = bool(pull.get("merged"))
    state = str(pull.get("state") or "")

    if state != "closed":
        return "ignored"

    return await settle(session, record, merged=merged)


async def settle(
    session: AsyncSession, record: PullRequest, *, merged: bool
) -> str:
    """Move the run now that the pull request's outcome is known.

    Shared by the webhook and the polling fallback, so the state machine does
    not care which of them delivered the news.
    """
    if record.state == "closed":
        # Replayed delivery. GitHub retries, and a retry must not re-run the
        # transition or emit a second timeline entry.
        return "already_settled"

    record.state = "closed"
    record.merged = merged
    record.merged_at = datetime.now(UTC) if merged else None
    await session.flush()

    run = await session.get(MigrationRun, record.migration_run_id)
    if run is None:  # pragma: no cover - foreign key makes this unreachable
        return "no_run"

    # Merged goes to VERIFIED: there is no post-merge verification environment
    # in this deployment, and claiming one would be the fake-green status the
    # project forbids. Closed-unmerged returns the project to monitoring.
    target = RunState.VERIFIED if merged else RunState.MONITORING_ACTIVE

    if not can_transition(run.state, target):
        logger.info(
            "continuity.merge_outcome_not_applied",
            extra={
                "migration_run_id": str(run.id),
                "from_state": run.state.value,
                "to_state": target.value,
            },
        )
        return "not_applicable"

    await transition(
        session,
        from_state=run.state,
        to_state=target,
        evidence=TransitionEvidence(
            reason=(
                f"pull request #{record.number} was "
                + ("merged" if merged else "closed without merging")
            ),
            actor="merge_detection",
            detail={"number": record.number, "merged": merged},
        ),
        project_id=run.project_id,
        migration_run_id=run.id,
    )
    run.state = target
    await session.flush()

    await _return_project_to_monitoring(session, run, merged=merged)

    await events.emit(
        session,
        kind=ActivityEventKind.MIGRATION_VERIFIED
        if merged
        else ActivityEventKind.PULL_REQUEST_CREATED,
        actor="merge_detection",
        summary=(
            f"Pull request #{record.number} "
            + ("was merged." if merged else "was closed without merging.")
        ),
        project_id=run.project_id,
        migration_run_id=run.id,
    )
    return "merged" if merged else "closed"


async def _return_project_to_monitoring(
    session: AsyncSession, run: MigrationRun, *, merged: bool
) -> None:
    """Put the project back to watching, now this run is finished either way."""
    project = await session.get(Project, run.project_id)
    if project is None:  # pragma: no cover
        return
    if can_transition(project.state, RunState.MONITORING_ACTIVE):
        await transition(
            session,
            from_state=project.state,
            to_state=RunState.MONITORING_ACTIVE,
            evidence=TransitionEvidence(
                reason=(
                    "migration merged; returning to monitoring"
                    if merged
                    else "migration closed unmerged; returning to monitoring"
                ),
                actor="merge_detection",
                detail={},
            ),
            project_id=project.id,
        )
        project.state = RunState.MONITORING_ACTIVE
        await session.flush()


def merge_detection_configured(settings: Settings) -> bool:
    """Whether this deployment can learn a pull request's outcome at all.

    Read by the UI. With neither webhook nor polling, a run stays in
    `MERGE_WAITING` forever, and saying so is better than a spinner that never
    resolves.
    """
    config = settings.github_app
    has_webhook = isinstance(config, GitHubAppConfig) and bool(config.webhook_secret)
    has_polling = isinstance(config, GitHubAppConfig)
    return has_webhook or has_polling
