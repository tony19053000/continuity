"""The adapter that makes a real provider monitorable at all.

Until this existed the default registry was empty, so every provider a project
depended on was recorded as "unmonitored" and the pipeline — fully built, fully
tested — could never start in a real deployment. These tests are about the two
things that adapter has to get right: reading a provider's own declared version,
and refusing to invent anything it was not given.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.models.enums import SourceKind
from backend.providers.base import (
    CapabilityNotSupported,
    ProviderCapability,
    ProviderFetchFailed,
    ProviderVersion,
)
from backend.providers.openapi_source import (
    UNVERSIONED,
    OpenApiSpecProvider,
    configured_providers,
)
from backend.providers.registry import ProviderRegistry

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "AcmePay", "version": "2.3.0"},
    "paths": {"/v1/charges": {"post": {"operationId": "createCharge"}}},
}


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(SPEC))
    return path


async def test_the_version_comes_from_the_providers_own_specification(
    spec_file: Path,
) -> None:
    adapter = OpenApiSpecProvider("acmepay", str(spec_file))

    version = await adapter.get_current_version()

    assert version.version == "2.3.0"
    assert version.is_current


async def test_a_spec_with_no_version_says_so_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """A provider that does not version its own spec cannot be diffed.

    Inventing "1.0.0" would make the next fetch look like a version change.
    """
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps({"openapi": "3.1.0", "info": {"title": "Acme"}}))

    version = await OpenApiSpecProvider("acme", str(path)).get_current_version()

    assert version.version == UNVERSIONED


async def test_the_spec_is_served_as_untrusted_external_content(
    spec_file: Path,
) -> None:
    adapter = OpenApiSpecProvider("acmepay", str(spec_file))

    document = await adapter.fetch_openapi_spec(ProviderVersion(version="2.3.0"))

    assert document.kind is SourceKind.OPENAPI_SPEC
    assert document.url == str(spec_file)
    assert json.loads(document.content) == SPEC
    # Content-addressed, so refetching an unchanged spec records no new version.
    assert document.content_hash


async def test_a_file_url_is_read_from_disk(spec_file: Path) -> None:
    """The form that makes the loop checkable by hand: edit a file, run a pass."""
    adapter = OpenApiSpecProvider("acmepay", f"file://{spec_file}")

    assert (await adapter.get_current_version()).version == "2.3.0"


async def test_editing_the_file_changes_the_reported_version(spec_file: Path) -> None:
    adapter = OpenApiSpecProvider("acmepay", str(spec_file))
    assert (await adapter.get_current_version()).version == "2.3.0"

    await asyncio.to_thread(
        spec_file.write_text,
        json.dumps({**SPEC, "info": {"title": "AcmePay", "version": "3.0.0"}}),
    )

    assert (await adapter.get_current_version()).version == "3.0.0"


async def test_a_missing_spec_fails_loudly(tmp_path: Path) -> None:
    adapter = OpenApiSpecProvider("acmepay", str(tmp_path / "absent.json"))

    with pytest.raises(ProviderFetchFailed, match="could not be read"):
        await adapter.get_current_version()


async def test_a_spec_that_is_not_json_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "openapi.json"
    path.write_text("<html>not a spec</html>")

    with pytest.raises(ProviderFetchFailed, match="not valid JSON"):
        await OpenApiSpecProvider("acmepay", str(path)).get_current_version()


async def test_it_declares_only_what_a_spec_url_can_support(spec_file: Path) -> None:
    """An adapter that declared a changelog would have to invent one."""
    adapter = OpenApiSpecProvider("acmepay", str(spec_file))

    assert adapter.capabilities == frozenset(
        {ProviderCapability.CURRENT_VERSION, ProviderCapability.OPENAPI_SPEC}
    )
    for unsupported in (
        adapter.fetch_changelog,
        adapter.get_version_history,
        adapter.fetch_sdk_release_info,
    ):
        with pytest.raises(CapabilityNotSupported):
            await unsupported()  # type: ignore[operator]


def test_configuration_becomes_adapters(tmp_path: Path) -> None:
    adapters = configured_providers(
        {"acmepay": str(tmp_path / "a.json"), "billing": "https://b.example/o.json"}
    )

    assert [adapter.provider_id for adapter in adapters] == ["acmepay", "billing"]


def test_an_empty_configuration_registers_nothing() -> None:
    """The honest default. It also means nothing is monitored."""
    assert configured_providers({}) == []
    assert configured_providers({"": "https://x.test/o.json"}) == []
    assert configured_providers({"acmepay": ""}) == []


def test_a_configured_adapter_resolves_through_the_registry(tmp_path: Path) -> None:
    """The registry is the seam every other stage resolves through."""
    registry = ProviderRegistry()
    for adapter in configured_providers({"acmepay": str(tmp_path / "a.json")}):
        registry.register(adapter)

    assert registry.try_get("acmepay") is not None
    assert registry.try_get("unregistered") is None
