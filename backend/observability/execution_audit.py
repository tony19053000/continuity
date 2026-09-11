"""Where execution records are persisted.

Kept out of `backend/shared/execution.py` deliberately: the executor is the
security boundary and has no database dependency, so it can be exercised — and
reasoned about — without one. This is the sink that turns its records into rows.

Both outcomes are recorded, and a refusal is the more interesting row of the
two. "What did this system try to do, and what stopped it" is the question the
audit table exists to answer (`backend/models/tables.py`, `AuditEvent`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AuditEvent
from backend.observability.logging import get_logger
from backend.shared.execution import ExecutionAuditRecord
from backend.shared.redaction import redact

logger = get_logger(__name__)

#: Audit kinds, closed rather than free-form so the security page can query them.
EXECUTION_RAN = "execution.ran"
EXECUTION_REFUSED = "execution.refused"


class DatabaseExecutionAudit:
    """Writes one `AuditEvent` row per execution attempt.

    Holds a session rather than opening its own: the audit row and whatever the
    caller is doing belong to the same transaction, so an execution cannot be
    recorded as having happened by a unit of work that then rolls back.
    """

    def __init__(self, session: AsyncSession, *, actor: str, project_id: uuid.UUID | None = None) -> None:
        self._session = session
        self._actor = actor
        self._project_id = project_id

    async def record(self, entry: ExecutionAuditRecord) -> None:
        refused = entry.refused_reason is not None

        event = AuditEvent(
            kind=EXECUTION_REFUSED if refused else EXECUTION_RAN,
            actor=self._actor,
            project_id=self._project_id,
            detail={
                # argv is Continuity's own construction, never model output, but
                # it is redacted anyway: an argument can carry a token, and this
                # table is read into a browser.
                "argv": [redact(argument) for argument in entry.argv],
                "cwd": entry.cwd,
                "status": entry.status,
                "exit_code": entry.exit_code,
                "duration_ms": entry.duration_ms,
                "stdout_excerpt": entry.stdout_excerpt,
                "stderr_excerpt": entry.stderr_excerpt,
                "truncated": entry.truncated,
                "refused_reason": entry.refused_reason,
            },
            occurred_at=datetime.now(UTC),
        )
        self._session.add(event)
        await self._session.flush()

        if refused:
            logger.warning(
                "continuity.execution_refused",
                extra={"actor": self._actor, "reason": entry.refused_reason},
            )
        else:
            logger.info(
                "continuity.execution_ran",
                extra={
                    "actor": self._actor,
                    "executable": entry.argv[0] if entry.argv else None,
                    "status": entry.status,
                    "exit_code": entry.exit_code,
                    "duration_ms": entry.duration_ms,
                },
            )


class NullExecutionAudit:
    """Records nothing. For tests that are not about auditing.

    Named so that using it is a visible choice. There is no default sink for
    exactly this reason: silently discarding audit is not something that should
    be reachable by omission.
    """

    def __init__(self) -> None:
        self.entries: list[ExecutionAuditRecord] = []

    async def record(self, entry: ExecutionAuditRecord) -> None:
        self.entries.append(entry)
