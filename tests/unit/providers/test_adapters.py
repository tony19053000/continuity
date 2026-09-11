"""C5-01: the provider adapter interface and registry.

The interface is the promise that Provider Lab and demo providers — built
outside this repository, per `CLAUDE.md` §3.13 — can plug in later. So these
tests exercise the *contract*, not any particular provider.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.models.enums import SourceKind
from backend.providers.base import (
    BaseProviderAdapter,
    CapabilityNotSupported,
    ExternalDocument,
    HttpProviderAdapter,
    ProviderCapability,
    ProviderFetchFailed,
    ProviderVersion,
)
from backend.providers.registry import ProviderNotRegistered, ProviderRegistry
from tests.support.provider_fixtures import (
    FixtureProviderAdapter,
    MinimalProviderAdapter,
)

#: Capability -> how to call the method that needs it.
CAPABILITY_CALLS: dict[ProviderCapability, Any] = {
    ProviderCapability.CURRENT_VERSION: lambda a: a.get_current_version(),
    ProviderCapability.VERSION_HISTORY: lambda a: a.get_version_history(),
    ProviderCapability.CHANGELOG: lambda a: a.fetch_changelog(),
    ProviderCapability.OPENAPI_SPEC: lambda a: a.fetch_openapi_spec(
        ProviderVersion(version="v1")
    ),
    ProviderCapability.DOCS: lambda a: a.fetch_docs("webhooks"),
    ProviderCapability.SDK_RELEASES: lambda a: a.fetch_sdk_release_info(),
    ProviderCapability.HEALTH_CHECK: lambda a: a.health_check(),
}


def test_every_capability_has_a_call_under_test() -> None:
    """Vacuity guard: a capability added later must be covered here."""
    assert set(CAPABILITY_CALLS) == set(ProviderCapability)


#: Everything `MinimalProviderAdapter` does not declare. Computed rather than
#: listed so a new capability is covered the moment it is added to the enum.
UNDECLARED = sorted(
    set(ProviderCapability) - MinimalProviderAdapter.capabilities, key=str
)


@pytest.mark.parametrize("capability", UNDECLARED, ids=lambda c: c.value)
async def test_an_undeclared_capability_refuses_rather_than_returning_nothing(
    capability: ProviderCapability,
) -> None:
    """C5-01 acceptance: capability gating, and *how* it fails.

    Returning `[]` or `None` for something a provider cannot do is the dangerous
    shape — "no changes found" and "I cannot look for changes" would become
    indistinguishable, and Continuity would report a provider as stable because
    it never checked it. Refusing loudly is the whole point.
    """
    adapter = MinimalProviderAdapter()

    with pytest.raises(CapabilityNotSupported) as raised:
        await CAPABILITY_CALLS[capability](adapter)

    assert adapter.provider_id in str(raised.value)
    assert capability.value in str(raised.value)


async def test_a_declared_capability_answers() -> None:
    adapter = MinimalProviderAdapter()

    version = await adapter.get_current_version()

    assert version.version == "2024-01-01"
    assert adapter.supports(ProviderCapability.CURRENT_VERSION)
    assert not adapter.supports(ProviderCapability.CHANGELOG)


async def test_declaring_a_capability_without_implementing_it_still_refuses() -> None:
    """Declaration alone is not implementation.

    An adapter that advertises a capability and inherits the base method would
    otherwise look supported to the registry and fail confusingly at call time.
    The base raises regardless — and with `NotImplementedError` rather than
    `CapabilityNotSupported`, because the two say different things to whoever
    reads the traceback: this provider cannot do it, versus this adapter forgot
    to write it.
    """

    class Overclaiming(BaseProviderAdapter):
        provider_id = "overclaiming"
        capabilities = frozenset({ProviderCapability.CHANGELOG})

    with pytest.raises(NotImplementedError, match=r"declares .* does not implement"):
        await Overclaiming().fetch_changelog()


# --- documents are content-addressed and attributable --------------------


def test_identical_content_hashes_identically() -> None:
    """The property the whole dedup story rests on."""

    def document(content: str, url: str) -> ExternalDocument:
        return ExternalDocument(
            kind=SourceKind.OPENAPI_SPEC, content=content, url=url, version="v1"
        )

    same = document("{}", "https://a.test/spec")
    same_content_other_url = document("{}", "https://b.test/spec")
    different = document('{"a": 1}', "https://a.test/spec")

    assert same.content_hash == same_content_other_url.content_hash
    assert same.content_hash != different.content_hash
    assert len(same.content_hash) == 64


def test_a_document_carries_a_usable_source_reference() -> None:
    document = ExternalDocument(
        kind=SourceKind.CHANGELOG,
        content="# notes",
        url="https://acmepay.test/changelog",
        version="v2",
    )

    source = document.source_ref()

    assert source.kind is SourceKind.CHANGELOG
    assert source.url == "https://acmepay.test/changelog"


# --- registry ------------------------------------------------------------


def test_the_registry_resolves_a_registered_adapter() -> None:
    registry = ProviderRegistry()
    adapter = FixtureProviderAdapter()

    registry.register(adapter)

    assert registry.get("acmepay") is adapter
    assert registry.ids() == frozenset({"acmepay"})


def test_an_unknown_provider_raises_and_says_what_is_known() -> None:
    """An unmonitored provider must be diagnosable, not a silent miss."""
    registry = ProviderRegistry()
    registry.register(FixtureProviderAdapter())

    with pytest.raises(ProviderNotRegistered) as raised:
        registry.get("stripe")

    assert "acmepay" in str(raised.value)


def test_try_get_returns_none_for_an_unknown_provider() -> None:
    """The monitor's path: most projects use providers nobody has adapted."""
    assert ProviderRegistry().try_get("stripe") is None


def test_registering_a_duplicate_id_is_refused_unless_replacement_is_explicit() -> None:
    """Silent replacement would let one adapter shadow another.

    Two adapters claiming `stripe` is a wiring bug. If the second silently won,
    monitoring would run against whichever module happened to import last.
    """
    registry = ProviderRegistry()
    registry.register(FixtureProviderAdapter())
    replacement = FixtureProviderAdapter()

    with pytest.raises(ValueError, match="acmepay"):
        registry.register(replacement)

    registry.register(replacement, replace=True)
    assert registry.get("acmepay") is replacement


def test_adapters_can_be_selected_by_capability() -> None:
    registry = ProviderRegistry()
    registry.register(FixtureProviderAdapter())
    registry.register(MinimalProviderAdapter())

    with_changelog = registry.with_capability(ProviderCapability.CHANGELOG)
    with_version = registry.with_capability(ProviderCapability.CURRENT_VERSION)

    assert [a.provider_id for a in with_changelog] == ["acmepay"]
    assert sorted(a.provider_id for a in with_version) == ["acmepay", "minimalpay"]


# --- HTTP base -----------------------------------------------------------


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, text: str) -> None:
        self._text = text
        self.timeouts: list[Any] = []

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.timeouts.append(kwargs.get("timeout"))
        return _Response(self._text)


async def test_an_oversized_response_is_refused_rather_than_parsed() -> None:
    """A provider serving an enormous body must not become a memory incident.

    Bounded before parsing, not after: the ceiling exists precisely so a
    hostile or broken response never reaches the parser.
    """

    class Adapter(HttpProviderAdapter):
        provider_id = "huge"
        max_response_bytes = 1_000

    adapter = Adapter(http=_Client("x" * 2_000))

    with pytest.raises(ProviderFetchFailed) as raised:
        await adapter._get_text("https://huge.test/spec")

    assert "1000" in str(raised.value) or "exceeded" in str(raised.value)


async def test_a_response_within_the_ceiling_is_returned() -> None:
    class Adapter(HttpProviderAdapter):
        provider_id = "small"
        max_response_bytes = 1_000

    adapter = Adapter(http=_Client("{}"))

    assert await adapter._get_text("https://small.test/spec") == "{}"
