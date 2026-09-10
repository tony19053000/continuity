"""C1-04 acceptance: activity events persist with the documented fields."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.models import ActivityEvent, ActivityEventKind, Confidence, EvidenceKind
from backend.models.schemas import Evidence
from backend.models.session import session_scope
from backend.observability.events import MAX_SUMMARY_LENGTH, emit
from tests.support.secret_samples import GITHUB_TOKEN


async def test_emit_persists_the_documented_fields(database: None) -> None:
    evidence = Evidence(
        kind=EvidenceKind.SOURCE,
        confidence=Confidence.CONFIRMED,
        file_path="payment_service.py",
        line_start=40,
        line_end=52,
    )
    async with session_scope() as session:
        await emit(
            session,
            kind=ActivityEventKind.PATCH_GENERATED,
            actor="migration_engineer",
            summary="Updated the webhook handler for the renamed event.",
            migration_run_id=None,
            agent_run_id=None,
            evidence=evidence,
        )

    async with session_scope() as session:
        stored = (await session.execute(select(ActivityEvent))).scalars().one()

    assert stored.kind is ActivityEventKind.PATCH_GENERATED
    assert stored.actor == "migration_engineer"
    assert stored.occurred_at is not None
    assert stored.evidence is not None
    assert stored.evidence["file_path"] == "payment_service.py"
    assert stored.evidence["line_start"] == 40
    assert stored.evidence["confidence"] == "confirmed"


async def test_a_secret_in_a_summary_is_filtered_before_storage(
    database: None,
) -> None:
    """This table is read straight into the browser."""
    secret = GITHUB_TOKEN

    async with session_scope() as session:
        event = await emit(
            session,
            kind=ActivityEventKind.VALIDATION_FAILED,
            actor="validator",
            summary=f"Request failed with token {secret}",
        )

    assert secret not in event.summary
    assert "[REDACTED]" in event.summary


async def test_a_long_summary_is_truncated(database: None) -> None:
    """A summary is a headline. Length here signals pasted-in model output."""
    async with session_scope() as session:
        event = await emit(
            session,
            kind=ActivityEventKind.MIGRATION_STARTED,
            actor="orchestrator",
            summary="x" * (MAX_SUMMARY_LENGTH * 2),
        )

    assert len(event.summary) == MAX_SUMMARY_LENGTH


async def test_an_unknown_event_kind_cannot_be_emitted(database: None) -> None:
    """The vocabulary is closed, so a typo fails loudly instead of vanishing."""
    async with session_scope() as session:
        with pytest.raises(ValueError):
            await emit(
                session,
                kind=ActivityEventKind("not_a_real_event"),
                actor="orchestrator",
                summary="should never persist",
            )
