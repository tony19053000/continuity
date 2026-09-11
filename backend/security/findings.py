"""Which findings are about the patch that is shipping.

A run makes several patches. When the Red Team breaks one (C9-01) or validation
fails, the next attempt replaces it — and the findings the rejected patch earned
stay in the table, because deleting evidence of what happened would be worse
than keeping it. But they are evidence, not a verdict: a CRITICAL finding
against a patch that no longer exists must not describe the code about to be
delivered, and must not be counted on a screen that claims to describe it.

`security_findings.attempt_number` records which patch a finding is about.
`None` means the finding is about the run rather than about one patch, and those
always count. Everything that reads findings to say something about the
*current* patch goes through here, so the delivery gate, the API, and the
migration report cannot drift apart on what "this run's findings" means.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import MigrationAttempt, SecurityFinding


def attempt_scope(delivered_attempt: int | None) -> ColumnElement[bool]:
    """A filter selecting only findings about `delivered_attempt`."""
    return or_(
        SecurityFinding.attempt_number.is_(None),
        SecurityFinding.attempt_number == delivered_attempt,
    )


async def latest_attempt_numbers(
    session: AsyncSession, run_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """The newest attempt number for each run, from rows.

    Runs with no attempt are absent rather than zero: "no attempt yet" and
    "attempt zero" are different states, and conflating them would make every
    finding look superseded.
    """
    ids = [run_id for run_id in run_ids if run_id is not None]
    if not ids:
        return {}

    rows = (
        await session.execute(
            select(
                MigrationAttempt.migration_run_id,
                func.max(MigrationAttempt.attempt_number),
            )
            .where(MigrationAttempt.migration_run_id.in_(ids))
            .group_by(MigrationAttempt.migration_run_id)
        )
    ).all()
    return {run_id: number for run_id, number in rows if run_id is not None}


def is_superseded(
    finding: SecurityFinding, latest: dict[uuid.UUID, int]
) -> bool:
    """Whether this finding is about a patch that was replaced.

    Surfaced rather than hidden. A reader deciding whether to trust a run wants
    to see that an earlier patch was rejected for a reason — labelled as
    history, not presented as a current defect.
    """
    if finding.attempt_number is None or finding.migration_run_id is None:
        return False
    newest = latest.get(finding.migration_run_id)
    return newest is not None and finding.attempt_number != newest


def current(
    findings: Sequence[SecurityFinding], latest: dict[uuid.UUID, int]
) -> list[SecurityFinding]:
    """Only the findings that describe the patch as it now stands."""
    return [row for row in findings if not is_superseded(row, latest)]
