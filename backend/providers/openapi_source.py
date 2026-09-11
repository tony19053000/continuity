"""A provider Continuity can actually monitor, configured rather than coded.

Until this existed, `default_registry` was empty. Every adapter in the
repository was either a test fixture or the evaluation harness's, so a real
deployment monitored nothing: `monitor_provider` looked up each provider a
project depends on, found no adapter, and recorded "unmonitored". The whole
pipeline was reachable and nothing ever reached it.

Most providers publish an OpenAPI specification at a stable URL and put their
version in it — `info.version` is part of the format. That is enough to monitor
one, and it needs no provider-specific code:

    PROVIDER_SPECS='{"acmepay": "https://acmepay.example/openapi.json"}'

A local path works too, which is the point for checking the loop by hand: point
a provider at a file, edit the file, and watch Continuity notice.

This is not a demo provider. It has no knowledge of any company — it fetches a
document from an address a deployment configured and reads two standard fields
out of it. `CLAUDE.md` rule 13 excludes Provider Lab and demo applications from
this repository, not the generic adapter that makes real ones monitorable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from backend.models.enums import SourceKind
from backend.observability.logging import get_logger
from backend.providers.base import (
    ExternalDocument,
    HttpProviderAdapter,
    ProviderCapability,
    ProviderFetchFailed,
    ProviderVersion,
)

logger = get_logger(__name__)

#: What a spec with no `info.version` is called. A provider that does not
#: version its own specification cannot be diffed between versions, and saying
#: so beats inventing "1.0.0".
UNVERSIONED: Final = "unversioned"


class OpenApiSpecProvider(HttpProviderAdapter):
    """Monitors one provider by its published OpenAPI specification.

    Declares exactly two capabilities, because that is exactly what a spec URL
    supports. No changelog, no SDK releases, no version history: an adapter that
    declared them would have to invent them, and `BaseProviderAdapter` raises
    rather than returning a plausible empty answer for a capability nobody
    implemented.
    """

    capabilities = frozenset(
        {ProviderCapability.CURRENT_VERSION, ProviderCapability.OPENAPI_SPEC}
    )

    def __init__(
        self,
        provider_id: str,
        spec_location: str,
        *,
        timeout_seconds: float = 30.0,
        http: Any = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, http=http)
        self.provider_id = provider_id
        self.spec_location = spec_location

    async def get_current_version(self) -> ProviderVersion:
        """The version the provider's own specification claims."""
        spec = await self._spec()
        info = spec.get("info")
        version = info.get("version") if isinstance(info, dict) else None
        return ProviderVersion(
            version=str(version) if version else UNVERSIONED, is_current=True
        )

    async def fetch_openapi_spec(self, version: ProviderVersion) -> ExternalDocument:
        """The specification as it is published right now.

        `version` is accepted and not used to select a document, because a spec
        URL serves one version — whatever is current. Fetching a historical
        version would need an adapter that knows how this provider publishes
        them, which is the provider-specific part this class deliberately does
        not have. Continuity compares against the *stored* baseline spec instead,
        which is a record it already holds.
        """
        text = await self._read()
        return ExternalDocument(
            kind=SourceKind.OPENAPI_SPEC,
            content=text,
            url=self.spec_location,
            version=version.version,
        )

    async def _spec(self) -> dict[str, Any]:
        text = await self._read()
        try:
            spec = json.loads(text)
        except ValueError as exc:
            raise ProviderFetchFailed(
                f"{self.spec_location} is not valid JSON.", url=self.spec_location
            ) from exc
        if not isinstance(spec, dict):
            raise ProviderFetchFailed(
                f"{self.spec_location} is not an OpenAPI document.",
                url=self.spec_location,
            )
        return spec

    async def _read(self) -> str:
        """Fetch over HTTP, or read a local file.

        Local paths are here for one reason: checking the loop by hand needs a
        provider whose specification you can edit. The location comes from
        deployment configuration, never from repository content or from a model,
        so this is not the untrusted-URL path that release verification guards
        (`03_SECURITY_ACCESS.md` §5).
        """
        parsed = urlparse(self.spec_location)
        if parsed.scheme in {"http", "https"}:
            return await self._get_text(self.spec_location)

        path = (
            Path(parsed.path) if parsed.scheme == "file" else Path(self.spec_location)
        )
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderFetchFailed(
                f"{self.spec_location} could not be read: {exc.strerror}",
                url=self.spec_location,
            ) from exc


def configured_providers(spec_locations: dict[str, str]) -> list[OpenApiSpecProvider]:
    """Build an adapter for each configured provider.

    Returns a list rather than registering, so a caller decides which registry
    they land in — the scheduler's, a test's, or the evaluation harness's.
    """
    return [
        OpenApiSpecProvider(provider_id, location)
        for provider_id, location in sorted(spec_locations.items())
        if provider_id and location
    ]
