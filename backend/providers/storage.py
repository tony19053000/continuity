"""Provider document storage.

Every fetched spec, changelog, and release listing is stored **content-addressed
and attributable**: a SHA-256 of the content is the identity, and a `SourceRef`
records the URL and the moment it was retrieved.

Content addressing is what makes monitoring idempotent. Polling a provider that
has not changed returns identical bytes, which hash to an existing row, which
produces no new document and therefore no spurious change event. Without it,
every poll would look like a change.

Stored content is **untrusted external text**. It is never executed, never
interpolated into an instruction region, and reaches an agent only inside a
delimited data block (`03_SECURITY_ACCESS.md` §5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Project, ProviderBaseline, ProviderSpec
from backend.models.schemas import SourceRef
from backend.observability.logging import get_logger
from backend.providers.base import ExternalDocument
from backend.providers.diff import MalformedSpec, parse_spec

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """A persisted provider document."""

    id: uuid.UUID
    provider_id: str
    version: str
    content_hash: str
    parsed_ok: bool
    parse_error: str | None
    was_new: bool
    source: SourceRef

    @property
    def is_usable_spec(self) -> bool:
        return self.parsed_ok


async def store_document(
    session: AsyncSession,
    provider_id: str,
    version: str,
    document: ExternalDocument,
) -> StoredDocument:
    """Persist a fetched document, deduplicating by content hash.

    A document that will not parse is stored with `parsed_ok=False` and its
    error, rather than being rejected outright. Keeping it is deliberate: the
    fact that a provider published something unreadable is itself useful, and
    silently coercing it would let a partial parse drive a diff.
    """
    content_hash = document.content_hash

    existing = (
        await session.execute(
            select(ProviderSpec).where(ProviderSpec.content_hash == content_hash)
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Identical content: the same document, not a new one. This is what
        # makes repeated polling produce no change events.
        return StoredDocument(
            id=existing.id,
            provider_id=existing.provider_id,
            version=existing.version,
            content_hash=existing.content_hash,
            parsed_ok=existing.parsed_ok,
            parse_error=existing.parse_error,
            was_new=False,
            source=SourceRef.model_validate(existing.source),
        )

    parsed_ok = True
    parse_error: str | None = None
    if document.kind.value == "openapi_spec":
        try:
            parse_spec(document.content)
        except MalformedSpec as exc:
            parsed_ok = False
            parse_error = str(exc)
            logger.warning(
                "continuity.provider_spec_unparseable",
                extra={"provider_id": provider_id, "version": version},
            )

    source = document.source_ref()
    record = ProviderSpec(
        provider_id=provider_id,
        version=version,
        content_hash=content_hash,
        content=document.content,
        source=source.model_dump(mode="json"),
        parsed_ok=parsed_ok,
        parse_error=parse_error,
    )
    session.add(record)
    await session.flush()

    return StoredDocument(
        id=record.id,
        provider_id=provider_id,
        version=version,
        content_hash=content_hash,
        parsed_ok=parsed_ok,
        parse_error=parse_error,
        was_new=True,
        source=source,
    )


async def latest_spec(
    session: AsyncSession, provider_id: str, version: str
) -> ProviderSpec | None:
    """The most recently stored *usable* spec for a provider version.

    Unparseable documents are stored but never returned here: callers diff with
    this result, and a half-readable document would produce a change set full of
    endpoints that only looked removed.
    """
    return (
        await session.execute(
            select(ProviderSpec)
            .where(
                ProviderSpec.provider_id == provider_id,
                ProviderSpec.version == version,
                ProviderSpec.parsed_ok.is_(True),
            )
            .order_by(ProviderSpec.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def baseline_version(
    session: AsyncSession, project: Project, provider_id: str
) -> str | None:
    """The version this project is currently known to work against."""
    return (
        await session.execute(
            select(ProviderBaseline.version).where(
                ProviderBaseline.project_id == project.id,
                ProviderBaseline.provider_id == provider_id,
            )
        )
    ).scalar_one_or_none()


async def advance_baseline(
    session: AsyncSession, project: Project, provider_id: str, version: str
) -> None:
    """Move a project's baseline forward after a verified migration.

    Called only once a migration is verified. Advancing earlier would make
    Continuity forget the version the project actually still runs against, and
    the next real change would go undetected.
    """
    baseline = (
        await session.execute(
            select(ProviderBaseline).where(
                ProviderBaseline.project_id == project.id,
                ProviderBaseline.provider_id == provider_id,
            )
        )
    ).scalar_one_or_none()

    if baseline is None:
        baseline = ProviderBaseline(
            project_id=project.id,
            provider_id=provider_id,
            version=version,
            established_at=datetime.now(UTC),
        )
        session.add(baseline)
    else:
        baseline.version = version
        baseline.established_at = datetime.now(UTC)

    await session.flush()
