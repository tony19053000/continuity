"""Activity event emission.

Activity events are the user-facing narrative of a run: what Continuity did,
when, and with what evidence. The UI timeline is a direct read of this table,
so an event that is not persisted never happened as far as the product is
concerned.

Two rules hold here:

* `kind` comes from the closed `ActivityEventKind` vocabulary, so the UI renders
  a known set of states rather than guessing at free-form strings.
* `summary` is a concise description of an action. Raw model reasoning is never
  persisted or displayed (`02_ARCHITECTURE.md` §16).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import ActivityEvent, ActivityEventKind
from backend.models.schemas import Evidence
from backend.observability.logging import get_logger
from backend.shared.redaction import redact

logger = get_logger(__name__)

# A summary is a headline, not a transcript. Anything longer is a sign that
# model output is being pasted in wholesale.
MAX_SUMMARY_LENGTH = 500


async def emit(
    session: AsyncSession,
    *,
    kind: ActivityEventKind,
    actor: str,
    summary: str,
    project_id: uuid.UUID | None = None,
    migration_run_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
    evidence: Evidence | None = None,
) -> ActivityEvent:
    """Persist one activity event.

    The summary is secret-filtered before storage — an event describing a tool
    call may quote an argument, and this table is read straight into the
    browser.
    """
    safe_summary = redact(summary)[:MAX_SUMMARY_LENGTH]

    event = ActivityEvent(
        project_id=project_id,
        migration_run_id=migration_run_id,
        agent_run_id=agent_run_id,
        kind=kind,
        actor=actor,
        summary=safe_summary,
        evidence=evidence.model_dump(mode="json") if evidence is not None else None,
        occurred_at=datetime.now(UTC),
    )
    session.add(event)
    await session.flush()

    logger.info(
        "continuity.activity",
        extra={
            "activity_kind": kind.value,
            "actor": actor,
            "project_id": str(project_id) if project_id else None,
        },
    )
    return event
