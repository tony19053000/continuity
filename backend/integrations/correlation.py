"""Connect a provider change to the code it touches. Deterministically.

**No model is involved.** Matching a changed endpoint against recorded call
sites is a lookup, and a lookup is something code does exactly. The Impact
Analyst (C6-03) decides whether a correlated change *matters*; this module only
establishes what it reaches, and everything it returns is CONFIRMED evidence
already in the graph.

The shape of the problem is that `ProviderChange.resource` and the graph speak
slightly different dialects. The differ emits `"POST /v2/charges request.currency"`;
the graph holds a call site whose recorded resource is `"/v1/charges"`. Bridging
those is the work here, and every bridge is explicit:

* path templates (`/v1/charges/{id}`, `/v1/charges/:id`) are compared
  segment-wise, so a parameter matches a concrete value but a different arity
  does not;
* a field-level change on a **renamed** endpoint is keyed to the new path, while
  the repository still calls the old one — so `correlate_all` builds a rename map
  from the change set and tries both. Without it, exactly the changes that
  break callers hardest would correlate to nothing;
* a change that reaches no code correlates to an empty result. That is an
  answer, not a failure, and it is the answer for most provider releases.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Final

from backend.integrations.graph import BlastRadius, IntegrationGraph
from backend.models import GraphNode
from backend.models.enums import ChangeType, EdgeKind, NodeKind
from backend.models.schemas import ProviderChange
from backend.observability.logging import get_logger

logger = get_logger(__name__)

#: `"POST /v2/charges request.currency"` -> method, path, and the rest.
_RESOURCE_PATTERN: Final = re.compile(
    r"^(?P<method>GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE)\s+(?P<path>/\S*)"
)

#: `{id}` and `:id` both mean "any single segment".
_TEMPLATE_SEGMENT: Final = re.compile(r"^(?:\{.*\}|:.+)$")

#: Changes that alter how every call to a provider is authenticated, and so
#: reach every call site rather than one endpoint.
_PROVIDER_WIDE: Final = frozenset(
    {ChangeType.AUTHENTICATION_CHANGED, ChangeType.OAUTH_SCOPE_CHANGED}
)


@dataclass(frozen=True, slots=True)
class Correlation:
    """What one provider change reaches in one project's graph."""

    change: ProviderChange
    call_sites: list[GraphNode] = field(default_factory=list)
    webhook_handlers: list[GraphNode] = field(default_factory=list)
    permissions: list[GraphNode] = field(default_factory=list)
    blast: BlastRadius | None = None
    #: Why each match was made, for evidence and for debugging a surprise.
    basis: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when the change touches nothing in this repository.

        The common case, and the one that stops Continuity opening a pull
        request for every provider release.
        """
        return not (self.call_sites or self.webhook_handlers or self.permissions)

    @property
    def seed_ids(self) -> set[uuid.UUID]:
        return {node.id for node in self.call_sites} | {
            node.id for node in self.webhook_handlers
        }

    def summary(self) -> dict[str, object]:
        """Compact, JSON-safe form for evidence and agent input."""
        return {
            "change_type": self.change.change_type.value,
            "resource": self.change.resource,
            "breaking": self.change.breaking,
            "call_sites": [node.key for node in self.call_sites],
            "webhook_handlers": [node.key for node in self.webhook_handlers],
            "permissions": [node.label for node in self.permissions],
            "basis": list(self.basis),
            "blast": self.blast.summary() if self.blast is not None else {},
        }


# ---------------------------------------------------------------------------
# Resource parsing
# ---------------------------------------------------------------------------


def parse_endpoint(resource: str) -> tuple[str, str] | None:
    """Pull `(METHOD, /path)` out of a change resource, if it names one."""
    match = _RESOURCE_PATTERN.match(resource.strip())
    if match is None:
        return None
    return match.group("method"), match.group("path")


def paths_match(left: str, right: str) -> bool:
    """Whether two API paths refer to the same endpoint.

    Segment-wise so that a template matches a concrete value, and so that
    `/v1/charges` never matches `/v1/charges/refunds` — different arity is a
    different endpoint, and treating them as one would send a migration at the
    wrong call site.
    """
    left_segments = [segment for segment in left.strip("/").split("/") if segment]
    right_segments = [segment for segment in right.strip("/").split("/") if segment]

    if len(left_segments) != len(right_segments):
        return False

    return all(
        _TEMPLATE_SEGMENT.match(a)
        or _TEMPLATE_SEGMENT.match(b)
        or a.casefold() == b.casefold()
        for a, b in zip(left_segments, right_segments, strict=True)
    )


def build_rename_map(changes: list[ProviderChange]) -> dict[str, str]:
    """new path -> old path, from the rename changes in a change set.

    Field-level changes on a renamed endpoint are keyed to the new operation,
    because that is what callers must satisfy. The repository, not yet migrated,
    still calls the old path. This is what lets both be true at once.
    """
    mapping: dict[str, str] = {}
    for change in changes:
        if change.change_type is not ChangeType.ENDPOINT_RENAMED:
            continue
        old = parse_endpoint(change.resource)
        new_operation = (change.new_contract or {}).get("operation")
        new = parse_endpoint(str(new_operation)) if new_operation else None
        if old and new:
            mapping[new[1]] = old[1]
    return mapping


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


async def correlate(
    graph: IntegrationGraph,
    version: int,
    provider_key: str,
    change: ProviderChange,
    *,
    rename_map: dict[str, str] | None = None,
) -> Correlation:
    """Find everything in the graph that one change touches."""
    renames = rename_map or {}

    call_sites: list[GraphNode] = []
    webhook_handlers: list[GraphNode] = []
    permissions: list[GraphNode] = []
    basis: list[str] = []

    if change.change_type is ChangeType.WEBHOOK_EVENT_CHANGED:
        webhook_handlers = await _handlers_for_event(
            graph, version, provider_key, change.resource
        )
        if webhook_handlers:
            basis.append(f"handles the {change.resource!r} webhook event")

    elif change.change_type in _PROVIDER_WIDE:
        permissions = await _matching_permissions(graph, version, provider_key, change)
        if permissions:
            basis.append("declares a scope named in the change")

        # An authentication change reaches every call: each one authenticates.
        # A scope change reaches them only when a scope was *removed*, which is
        # what `breaking` records — a widened scope breaks no existing call.
        if change.change_type is ChangeType.AUTHENTICATION_CHANGED or change.breaking:
            call_sites = await graph.call_sites_for_provider(version, provider_key)
            if call_sites:
                basis.append("every call to this provider authenticates")

    elif change.change_type is ChangeType.ENUM_CHANGED:
        call_sites = await _call_sites_using_values(graph, version, provider_key, change)
        if call_sites:
            basis.append("passes a value removed from the enum")

    else:
        endpoint = parse_endpoint(change.resource)
        if endpoint is not None:
            _, path = endpoint
            candidates = [path]
            if path in renames:
                # Keyed to the new path; the repository still calls the old one.
                candidates.append(renames[path])
            call_sites = await _call_sites_for_paths(
                graph, version, provider_key, candidates
            )
            if call_sites:
                basis.append(f"calls {' or '.join(candidates)}")

    blast = None
    seed_ids = {node.id for node in call_sites} | {node.id for node in webhook_handlers}
    if seed_ids:
        blast = await graph.blast_radius(version, seed_ids)

    correlation = Correlation(
        change=change,
        call_sites=call_sites,
        webhook_handlers=webhook_handlers,
        permissions=permissions,
        blast=blast,
        basis=basis,
    )

    # Logged at debug, not warning: a change reaching nothing is the normal
    # outcome for most provider releases, and warning about it would train
    # everyone to ignore the log.
    logger.debug(
        "continuity.change_correlated",
        extra={
            "change_type": change.change_type.value,
            "resource": change.resource,
            "call_sites": len(call_sites),
            "webhook_handlers": len(webhook_handlers),
            "permissions": len(permissions),
        },
    )
    return correlation


async def correlate_all(
    graph: IntegrationGraph,
    version: int,
    provider_key: str,
    changes: list[ProviderChange],
) -> list[Correlation]:
    """Correlate a whole change set, sharing the rename map across it."""
    rename_map = build_rename_map(changes)
    return [
        await correlate(graph, version, provider_key, change, rename_map=rename_map)
        for change in changes
    ]


# ---------------------------------------------------------------------------
# Graph lookups
# ---------------------------------------------------------------------------


async def _call_sites_for_paths(
    graph: IntegrationGraph, version: int, provider_key: str, paths: list[str]
) -> list[GraphNode]:
    """Call sites whose recorded resource matches any of these paths."""
    matched = []
    for node in await graph.call_sites_for_provider(version, provider_key):
        recorded = (node.attributes or {}).get("resource")
        if isinstance(recorded, str) and any(
            paths_match(recorded, path) for path in paths
        ):
            matched.append(node)
    return matched


async def _handlers_for_event(
    graph: IntegrationGraph, version: int, provider_key: str, event: str
) -> list[GraphNode]:
    """Symbols handling one named webhook event.

    Narrower than `webhook_handlers_for_provider`, which returns every handler:
    a change to `payment.paid` does not affect the handler for `invoice.sent`,
    and treating it as though it did would put unrelated code in a migration.
    """
    provider = await graph.node(version, NodeKind.PROVIDER, provider_key)
    if provider is None:
        return []

    handler_ids = set()
    for edge in await graph.edges(version, kind=EdgeKind.HANDLES_WEBHOOK_EVENT):
        if edge.target_node_id != provider.id:
            continue
        events = (edge.attributes or {}).get("events")
        if isinstance(events, list) and any(
            str(candidate).casefold() == event.casefold() for candidate in events
        ):
            handler_ids.add(edge.source_node_id)

    if not handler_ids:
        return []

    handlers = await graph.webhook_handlers_for_provider(version, provider_key)
    return [node for node in handlers if node.id in handler_ids]


async def _matching_permissions(
    graph: IntegrationGraph, version: int, provider_key: str, change: ProviderChange
) -> list[GraphNode]:
    """Permission nodes naming a scope that appears in the change."""
    named = _scopes_in(change)
    permissions = await graph.permissions_for_provider(version, provider_key)

    if not named:
        # An authentication change with no scope list reaches every declared
        # permission — there is no basis for narrowing it.
        return permissions

    return [
        node
        for node in permissions
        if str((node.attributes or {}).get("scope", node.label)).casefold() in named
    ]


def _enum_values(contract: dict[str, object] | None) -> set[str]:
    values = (contract or {}).get("values")
    if not isinstance(values, list):
        return set()
    return {str(value).casefold() for value in values if isinstance(value, str)}


def _scopes_in(change: ProviderChange) -> set[str]:
    scopes: set[str] = set()
    for contract in (change.old_contract, change.new_contract):
        values = (contract or {}).get("scopes")
        if isinstance(values, list):
            scopes.update(str(value).casefold() for value in values)
    return scopes


async def _call_sites_using_values(
    graph: IntegrationGraph, version: int, provider_key: str, change: ProviderChange
) -> list[GraphNode]:
    """Call sites passing a literal that the enum no longer accepts.

    Only *removed* values. An enum that gained a member breaks nothing, and
    correlating on additions would flag every call site that mentions the enum
    at all.
    """
    removed = _enum_values(change.old_contract) - _enum_values(change.new_contract)
    if not removed:
        return []

    matched = []
    for node in await graph.call_sites_for_provider(version, provider_key):
        arguments = (node.attributes or {}).get("arguments")
        if not isinstance(arguments, list):
            continue
        rendered = " ".join(str(argument).casefold() for argument in arguments)
        if any(value in rendered for value in removed):
            matched.append(node)
    return matched
