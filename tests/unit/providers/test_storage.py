"""C5-02/C5-05: content-addressed provider document storage.

Storage is what makes repeated polling idempotent. If the same document stored
twice produced two rows, every poll would look like a new version and the
change pipeline would fire forever.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select

from backend.models import Project, ProviderBaseline, ProviderSpec, Repository, User
from backend.models.enums import SourceKind
from backend.models.session import session_scope
from backend.providers.base import ExternalDocument
from backend.providers.storage import (
    advance_baseline,
    baseline_version,
    latest_spec,
    store_document,
)
from tests.support.provider_fixtures import spec_v1, spec_v2

pytestmark = pytest.mark.usefixtures("database")


def _document(content: str, *, kind: SourceKind = SourceKind.OPENAPI_SPEC) -> ExternalDocument:
    return ExternalDocument(
        kind=kind,
        content=content,
        url="https://acmepay.test/openapi/v1.json",
        version="v1",
    )


async def _project() -> Project:
    async with session_scope() as session:
        user = User(google_subject=f"sub-{uuid.uuid4()}", email="owner@example.test")
        session.add(user)
        await session.flush()

        repository = Repository(owner="acme", name=f"app-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()

        project = Project(
            user_id=user.id, repository_id=repository.id, name="commerce-api"
        )
        session.add(project)
        await session.flush()
        await session.refresh(project)
        return project


async def test_a_document_is_stored_once_and_reported_as_new() -> None:
    async with session_scope() as session:
        stored = await store_document(session, "acmepay", "v1", _document(json.dumps(spec_v1())))

    assert stored.was_new is True
    assert stored.parsed_ok is True
    assert stored.parse_error is None
    assert len(stored.content_hash) == 64


async def test_storing_identical_content_twice_creates_exactly_one_row() -> None:
    """C5-05 acceptance, at the storage layer.

    This is the mechanism behind "polling the same version repeatedly produces
    exactly one change event" — the second fetch is recognised as the same
    document rather than a new one.
    """
    content = json.dumps(spec_v1())

    async with session_scope() as session:
        first = await store_document(session, "acmepay", "v1", _document(content))
        second = await store_document(session, "acmepay", "v1", _document(content))
        rows = (
            await session.execute(
                select(func.count()).select_from(ProviderSpec).where(
                    ProviderSpec.provider_id == "acmepay"
                )
            )
        ).scalar_one()

    assert first.was_new is True
    assert second.was_new is False
    assert first.content_hash == second.content_hash
    assert first.id == second.id
    assert rows == 1


async def test_genuinely_different_content_is_stored_separately() -> None:
    async with session_scope() as session:
        first = await store_document(session, "acmepay", "v1", _document(json.dumps(spec_v1())))
        second = await store_document(session, "acmepay", "v2", _document(json.dumps(spec_v2())))

    assert second.was_new is True
    assert first.content_hash != second.content_hash


async def test_dedup_is_by_content_not_by_version_label() -> None:
    """A provider that bumps its version label without changing anything.

    Real providers republish specs under new labels all the time. Keying on the
    label would manufacture a change set for a document that did not change.
    """
    content = json.dumps(spec_v1())

    async with session_scope() as session:
        first = await store_document(session, "acmepay", "2024-01-01", _document(content))
        second = await store_document(session, "acmepay", "2024-06-01", _document(content))

    assert second.was_new is False
    assert second.id == first.id


async def test_an_unparseable_spec_is_kept_and_marked_rather_than_dropped() -> None:
    """Fails closed without losing the evidence.

    That a provider published something unreadable is itself a finding. Storing
    it with `parsed_ok=False` keeps it diagnosable; rejecting it outright would
    leave no trace of why monitoring stalled.
    """
    async with session_scope() as session:
        stored = await store_document(
            session, "acmepay", "v1", _document("<html>504 Gateway Timeout</html>")
        )
        row = (
            await session.execute(
                select(ProviderSpec).where(ProviderSpec.content_hash == stored.content_hash)
            )
        ).scalar_one()
        usable = await latest_spec(session, "acmepay", "v1")

    assert stored.was_new is True
    assert stored.parsed_ok is False
    assert stored.is_usable_spec is False
    assert stored.parse_error

    # Kept, with the reason recorded...
    assert row.parsed_ok is False
    assert row.parse_error
    # ...but never handed to a differ, which is the half that matters.
    assert usable is None


async def test_a_non_spec_document_is_not_parse_checked() -> None:
    """A changelog is prose. Requiring it to parse as a spec would reject it."""
    async with session_scope() as session:
        stored = await store_document(
            session,
            "acmepay",
            "v2",
            _document("# Changelog\n\n- things changed", kind=SourceKind.CHANGELOG),
        )

    assert stored.parsed_ok is True


async def test_latest_spec_returns_nothing_before_anything_is_stored() -> None:
    async with session_scope() as session:
        assert await latest_spec(session, "acmepay", "v1") is None


# --- baselines -----------------------------------------------------------


async def test_a_project_with_no_baseline_reports_none() -> None:
    """The first-sight case: nothing to diff against yet, and no pretending."""
    project = await _project()

    async with session_scope() as session:
        assert await baseline_version(session, project, "acmepay") is None


async def test_advancing_a_baseline_records_the_new_version() -> None:
    project = await _project()

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v1")

    async with session_scope() as session:
        assert await baseline_version(session, project, "acmepay") == "v1"


async def test_advancing_an_existing_baseline_moves_it_rather_than_duplicating() -> None:
    """Two baselines for one provider would make "current version" ambiguous."""
    project = await _project()

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v1")
    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v2")

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(func.count()).select_from(ProviderBaseline).where(
                    ProviderBaseline.project_id == project.id,
                    ProviderBaseline.provider_id == "acmepay",
                )
            )
        ).scalar_one()
        assert await baseline_version(session, project, "acmepay") == "v2"

    assert rows == 1


async def test_baselines_are_scoped_per_project() -> None:
    """Two projects can sit on different versions of the same provider."""
    first = await _project()
    second = await _project()

    async with session_scope() as session:
        await advance_baseline(session, first, "acmepay", "v1")
        await advance_baseline(session, second, "acmepay", "v2")

    async with session_scope() as session:
        assert await baseline_version(session, first, "acmepay") == "v1"
        assert await baseline_version(session, second, "acmepay") == "v2"
