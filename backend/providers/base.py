"""Provider adapter interface.

Continuity is never hardcoded to one provider. An adapter declares which
capabilities it has, and callers gate on them: asking an adapter for a changelog
it cannot fetch raises `CapabilityNotSupported` rather than returning an empty
result that would be indistinguishable from "this provider published nothing".

That distinction matters more than it looks. "No changes" and "we could not
check" lead to opposite actions, and a provider integration that silently
reports the first when it means the second is worse than one that fails.

**Everything an adapter returns is untrusted external content.** A changelog is
written by a third party and may contain anything, including text aimed at the
agents that will read it. Adapters return `ExternalDocument`, which carries its
provenance and is only ever placed inside a delimited data block
(`03_SECURITY_ACCESS.md` §5).

Adding a provider means implementing this protocol and registering it. It
requires no change to monitoring, the graph, impact analysis, or any agent —
proven by a test that registers a new adapter and drives it end to end without
touching a module outside `backend/providers/`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from backend.models.enums import SourceKind
from backend.models.schemas import SourceRef
from backend.shared.errors import ContinuityError


class ProviderCapability(StrEnum):
    """What an adapter can actually do.

    Declared rather than discovered, because discovery would mean calling a
    provider to find out it cannot answer — a network round trip to learn
    something the adapter author already knows.
    """

    CURRENT_VERSION = "current_version"
    VERSION_HISTORY = "version_history"
    CHANGELOG = "changelog"
    OPENAPI_SPEC = "openapi_spec"
    DOCS = "docs"
    SDK_RELEASES = "sdk_releases"
    HEALTH_CHECK = "health_check"


class CapabilityNotSupported(ContinuityError):
    """An adapter was asked for something it does not provide.

    Raised rather than returning empty, because "nothing changed" and "we could
    not check" demand different responses and must never be confused.
    """

    code = "capability_not_supported"
    status_code = 501
    message = "This provider adapter does not support that capability."

    def __init__(self, provider_id: str, capability: ProviderCapability) -> None:
        super().__init__(
            f"Adapter {provider_id!r} does not support {capability.value}.",
            provider_id=provider_id,
            capability=capability.value,
        )


class ProviderFetchFailed(ContinuityError):
    """A provider source could not be reached or returned something unusable.

    Distinct from `CapabilityNotSupported`: the adapter *can* do this, and the
    attempt failed. A monitoring run records the failure and retries later
    rather than concluding the provider has not changed.
    """

    code = "provider_fetch_failed"
    status_code = 502
    message = "The provider source could not be fetched."


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Who the provider is, independent of how Continuity reaches it."""

    provider_id: str
    display_name: str
    homepage: str | None = None
    documentation_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderVersion:
    """One published version of a provider's API."""

    version: str
    released_at: datetime | None = None
    is_current: bool = False

    def __str__(self) -> str:
        return self.version


@dataclass(frozen=True, slots=True)
class ExternalDocument:
    """Untrusted content fetched from a provider, with its provenance.

    `content_hash` is what makes storage content-addressed: refetching an
    unchanged document produces the same hash and therefore no new row and no
    spurious change event.
    """

    kind: SourceKind
    content: str
    url: str | None = None
    version: str | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def source_ref(self) -> SourceRef:
        """Provenance for anything derived from this document."""
        return SourceRef(
            kind=self.kind,
            url=self.url,
            document_hash=self.content_hash,
            retrieved_at=self.retrieved_at,
        )


@dataclass(frozen=True, slots=True)
class SdkRelease:
    """A published SDK version, from a package registry."""

    package: str
    version: str
    released_at: datetime | None = None
    yanked: bool = False
    deprecated: bool = False


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    reachable: bool
    detail: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class ProviderAdapter(Protocol):
    """One external provider Continuity can monitor.

    Implement only the methods your `capabilities` declare. The base class
    below raises `CapabilityNotSupported` for the rest, so a partial adapter is
    a normal thing to write rather than a special case.
    """

    provider_id: str
    capabilities: frozenset[ProviderCapability]

    def get_identity(self) -> ProviderIdentity: ...
    async def get_current_version(self) -> ProviderVersion: ...
    async def get_version_history(self) -> list[ProviderVersion]: ...
    async def fetch_changelog(self, since: ProviderVersion | None = None) -> ExternalDocument: ...
    async def fetch_openapi_spec(self, version: ProviderVersion) -> ExternalDocument: ...
    async def fetch_docs(self, topic: str) -> ExternalDocument: ...
    async def fetch_sdk_release_info(self) -> list[SdkRelease]: ...
    async def health_check(self) -> ProviderHealth: ...


class BaseProviderAdapter:
    """Convenience base: every capability refuses unless declared and overridden.

    Subclasses override what they support. Anything they do not override raises,
    so an adapter cannot accidentally return a plausible empty answer for a
    capability it never implemented.
    """

    provider_id: str = "unset"
    capabilities: frozenset[ProviderCapability] = frozenset()

    def supports(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities

    def _require(self, capability: ProviderCapability) -> None:
        if capability not in self.capabilities:
            raise CapabilityNotSupported(self.provider_id, capability)

    def get_identity(self) -> ProviderIdentity:
        return ProviderIdentity(provider_id=self.provider_id, display_name=self.provider_id)

    async def get_current_version(self) -> ProviderVersion:
        self._require(ProviderCapability.CURRENT_VERSION)
        raise NotImplementedError(
            f"{type(self).__name__} declares CURRENT_VERSION but does not implement it."
        )

    async def get_version_history(self) -> list[ProviderVersion]:
        self._require(ProviderCapability.VERSION_HISTORY)
        raise NotImplementedError(
            f"{type(self).__name__} declares VERSION_HISTORY but does not implement it."
        )

    async def fetch_changelog(
        self, since: ProviderVersion | None = None
    ) -> ExternalDocument:
        self._require(ProviderCapability.CHANGELOG)
        raise NotImplementedError(
            f"{type(self).__name__} declares CHANGELOG but does not implement it."
        )

    async def fetch_openapi_spec(self, version: ProviderVersion) -> ExternalDocument:
        self._require(ProviderCapability.OPENAPI_SPEC)
        raise NotImplementedError(
            f"{type(self).__name__} declares OPENAPI_SPEC but does not implement it."
        )

    async def fetch_docs(self, topic: str) -> ExternalDocument:
        self._require(ProviderCapability.DOCS)
        raise NotImplementedError(
            f"{type(self).__name__} declares DOCS but does not implement it."
        )

    async def fetch_sdk_release_info(self) -> list[SdkRelease]:
        self._require(ProviderCapability.SDK_RELEASES)
        raise NotImplementedError(
            f"{type(self).__name__} declares SDK_RELEASES but does not implement it."
        )

    async def health_check(self) -> ProviderHealth:
        self._require(ProviderCapability.HEALTH_CHECK)
        raise NotImplementedError(
            f"{type(self).__name__} declares HEALTH_CHECK but does not implement it."
        )


class HttpProviderAdapter(BaseProviderAdapter):
    """Base for adapters that fetch over HTTP.

    Bounds every request: a timeout and a response size cap
    (`03_SECURITY_ACCESS.md` — adapter fetches are network-bounded). A provider
    serving an endless response must not be able to hang or exhaust a monitoring
    worker.
    """

    #: Refuse a response larger than this. A spec is a document, not a stream.
    max_response_bytes: int = 8_000_000

    def __init__(self, *, timeout_seconds: float = 30.0, http: Any = None) -> None:
        self._timeout = timeout_seconds
        self._http = http

    async def _get_text(self, url: str) -> str:
        """Fetch a URL as text, bounded by time and size."""
        import httpx

        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.get(url, follow_redirects=True)
            if response.status_code >= 400:
                raise ProviderFetchFailed(
                    f"{url} returned {response.status_code}.", url=url
                )

            content = response.text
            if len(content.encode("utf-8", errors="ignore")) > self.max_response_bytes:
                raise ProviderFetchFailed(
                    f"{url} exceeded {self.max_response_bytes} bytes.", url=url
                )
            return content
        except ProviderFetchFailed:
            raise
        except Exception as exc:
            raise ProviderFetchFailed(f"{url} could not be fetched.", url=url) from exc
        finally:
            if self._http is None:
                await client.aclose()
