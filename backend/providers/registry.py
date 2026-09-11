"""Provider adapter registry.

Adapters register by id. Monitoring, storage, diffing, and the Change Scout all
resolve adapters through here, so adding a provider is a registration rather
than an edit to the pipeline.

This is the seam an externally-built Provider Lab plugs into: implement
`ProviderAdapter`, call `register`, and the rest of Continuity works unchanged.
A test proves that claim by registering a new adapter and driving it through
monitoring without touching a module outside `backend/providers/`.
"""

from __future__ import annotations

from backend.providers.base import ProviderAdapter, ProviderCapability
from backend.shared.errors import ContinuityError


class ProviderNotRegistered(ContinuityError):
    """No adapter claims this provider id."""

    code = "provider_not_registered"
    status_code = 404
    message = "No adapter is registered for that provider."

    def __init__(self, provider_id: str, known: list[str]) -> None:
        super().__init__(
            f"No adapter registered for {provider_id!r}. Known: {known or 'none'}.",
            provider_id=provider_id,
        )


class ProviderRegistry:
    """Name → adapter."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter, *, replace: bool = False) -> None:
        """Register an adapter under its own `provider_id`.

        Re-registering raises unless `replace` is explicit, so two adapters
        claiming the same provider is a loud conflict rather than a silent
        last-one-wins.
        """
        provider_id = adapter.provider_id
        if not provider_id or provider_id == "unset":
            raise ValueError(f"{type(adapter).__name__} must set a provider_id.")

        if provider_id in self._adapters and not replace:
            raise ValueError(
                f"An adapter is already registered for {provider_id!r}. "
                "Pass replace=True to override deliberately."
            )

        self._adapters[provider_id] = adapter

    def unregister(self, provider_id: str) -> None:
        self._adapters.pop(provider_id, None)

    def get(self, provider_id: str) -> ProviderAdapter:
        try:
            return self._adapters[provider_id]
        except KeyError:
            raise ProviderNotRegistered(provider_id, sorted(self._adapters)) from None

    def try_get(self, provider_id: str) -> ProviderAdapter | None:
        """Resolve without raising.

        Most projects depend on providers Continuity has no adapter for. That is
        normal, not an error — the provider is simply unmonitored until someone
        writes one.
        """
        return self._adapters.get(provider_id)

    def ids(self) -> frozenset[str]:
        return frozenset(self._adapters)

    def with_capability(self, capability: ProviderCapability) -> list[ProviderAdapter]:
        """Adapters able to do a given thing, for capability-driven scheduling."""
        return [
            adapter
            for _id, adapter in sorted(self._adapters.items())
            if capability in adapter.capabilities
        ]

    def clear(self) -> None:
        """Empty the registry. For tests; never called in production paths."""
        self._adapters.clear()


#: Process-wide registry.
registry = ProviderRegistry()
