"""Integration Intelligence Graph: persistence and queries.

The graph is what makes Continuity more than a dependency bot. Knowing that
`acmepay` is installed is trivia; knowing that `create_payment()` in
`payment_service.py` implements Checkout and is covered by `test_payments.py` is
what lets the Impact Analyst say "this change breaks Checkout" and prove it.

Two properties are load-bearing:

* **Versioned, immutable per scan.** Each scan writes a new `graph_version`;
  queries read one version. History is therefore free, and a scan can never
  corrupt the graph a running migration is reading.
* **Every node and edge carries `Evidence` and a confidence.** `CONFIRMED` means
  derived deterministically from the index; `INFERRED` means a model proposed
  it. The distinction survives all the way to the UI, and inferred data may
  never overwrite confirmed data (`02_ARCHITECTURE.md` §5).

`blast_radius` is the query the whole structure exists for: given the call sites
a provider change touches, what workflows and tests are downstream?
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Confidence, EdgeKind, GraphEdge, GraphNode, NodeKind
from backend.models.schemas import Evidence
from backend.security.secret_filter import redact

#: Edges traversed when computing blast radius, in the direction that means
#: "downstream of".
#:
#: `COVERED_BY_TEST` is written SYMBOL → TEST by the extractor, so finding the
#: tests that cover a symbol is a *forward* traversal. An earlier version listed
#: it as reverse, which silently returned no tests for every blast radius — the
#: query looked like it worked and quietly answered "nothing covers this".
_FORWARD_EDGES = frozenset(
    {
        EdgeKind.IMPLEMENTS_WORKFLOW,
        EdgeKind.CALLS_PROVIDER,
        EdgeKind.HANDLES_WEBHOOK_EVENT,
        EdgeKind.REQUIRES_PERMISSION,
        EdgeKind.DEFINED_IN,
        EdgeKind.COVERED_BY_TEST,
    }
)
#: No edge currently needs reverse traversal. Kept so the distinction stays
#: explicit rather than being rediscovered when one does.
_REVERSE_EDGES: frozenset[EdgeKind] = frozenset()


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """A node to write. `key` is stable across versions for the same entity.

    `evidence` is required, not optional. C4-01 promises every persisted node
    can be traced to a file, a line, or a manifest, and a default of `None`
    would make that a convention every future caller has to remember rather
    than something the type system enforces.
    """

    kind: NodeKind
    key: str
    label: str
    confidence: Confidence
    evidence: Evidence
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """An edge to write, addressed by the `(kind, key)` of its endpoints.

    Addressing by key rather than by database id means a caller can describe a
    whole graph before any of it exists, which is how extraction produces one
    unit of work rather than a dependency-ordered sequence of writes.
    """

    kind: EdgeKind
    source: tuple[NodeKind, str]
    target: tuple[NodeKind, str]
    confidence: Confidence
    evidence: Evidence
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphDelta:
    """A set of nodes and edges to apply as one version."""

    nodes: list[NodeSpec] = field(default_factory=list)
    edges: list[EdgeSpec] = field(default_factory=list)

    def merged_with(self, other: GraphDelta) -> GraphDelta:
        return GraphDelta(nodes=[*self.nodes, *other.nodes], edges=[*self.edges, *other.edges])


class ConfirmedNodeOverwrite(ValueError):
    """An inferred node tried to replace a confirmed one.

    Raised rather than silently ignored: a model contradicting deterministic
    analysis is a signal worth surfacing, not a no-op.
    """


def _evidence_payload(evidence: Evidence) -> dict[str, object]:
    """Serialise evidence, secret-filtering the excerpt on the way in.

    Filtering here rather than at read time means a secret never reaches storage
    at all, so a later reader cannot forget to filter.
    """
    payload = evidence.model_dump(mode="json")
    if payload.get("excerpt"):
        payload["excerpt"] = redact(str(payload["excerpt"]))
    return payload


class IntegrationGraph:
    """Reads and writes one project's graph."""

    def __init__(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        self._session = session
        self._project_id = project_id

    # --- versions ---

    async def latest_version(self) -> int | None:
        """The newest version written, or None if this project has never scanned."""
        return (
            await self._session.execute(
                select(func.max(GraphNode.graph_version)).where(
                    GraphNode.project_id == self._project_id
                )
            )
        ).scalar_one_or_none()

    async def next_version(self) -> int:
        current = await self.latest_version()
        return 1 if current is None else current + 1

    # --- writing ---

    async def apply(
        self, delta: GraphDelta, *, version: int, allow_overwrite_confirmed: bool = False
    ) -> dict[tuple[NodeKind, str], uuid.UUID]:
        """Write a delta into `version`, returning the node ids by key.

        Nodes are written before edges so an edge can always resolve its
        endpoints. A node key already present in this version is updated rather
        than duplicated — except that an `INFERRED` node may never replace a
        `CONFIRMED` one, which is the rule that keeps model output from
        overwriting deterministic fact.
        """
        existing = await self._nodes_by_key(version)
        ids: dict[tuple[NodeKind, str], uuid.UUID] = {
            (node.kind, node.key): node.id for node in existing.values()
        }

        for spec in delta.nodes:
            identity = (spec.kind, spec.key)
            current = existing.get(identity)

            if current is not None:
                if (
                    current.confidence is Confidence.CONFIRMED
                    and spec.confidence is Confidence.INFERRED
                    and not allow_overwrite_confirmed
                ):
                    raise ConfirmedNodeOverwrite(
                        f"inferred node {spec.kind.value}:{spec.key!r} would overwrite "
                        "a confirmed node"
                    )
                current.label = spec.label
                current.attributes = dict(spec.attributes) or None
                current.evidence = _evidence_payload(spec.evidence)
                continue

            node = GraphNode(
                project_id=self._project_id,
                graph_version=version,
                kind=spec.kind,
                key=spec.key,
                label=spec.label,
                attributes=dict(spec.attributes) or None,
                confidence=spec.confidence,
                evidence=_evidence_payload(spec.evidence),
            )
            self._session.add(node)
            await self._session.flush()
            existing[identity] = node
            ids[identity] = node.id

        known_edges = await self._edge_keys(version)

        # Distinct names from the node loop above. Reusing `spec`/`identity`
        # worked but bound two different shapes to one name, which is how a
        # later edit silently uses the wrong one.
        for edge_spec in delta.edges:
            source_id = ids.get(edge_spec.source)
            target_id = ids.get(edge_spec.target)
            if source_id is None or target_id is None:
                # A dangling edge is a bug in the producer, not something to
                # persist and puzzle over later.
                missing = edge_spec.source if source_id is None else edge_spec.target
                raise ValueError(
                    f"edge {edge_spec.kind.value} references a missing node: {missing}"
                )

            edge_identity = (edge_spec.kind, source_id, target_id)
            if edge_identity in known_edges:
                continue

            self._session.add(
                GraphEdge(
                    project_id=self._project_id,
                    graph_version=version,
                    kind=edge_spec.kind,
                    source_node_id=source_id,
                    target_node_id=target_id,
                    attributes=dict(edge_spec.attributes) or None,
                    confidence=edge_spec.confidence,
                    evidence=_evidence_payload(edge_spec.evidence),
                )
            )
            known_edges.add(edge_identity)

        await self._session.flush()
        return ids

    async def _nodes_by_key(self, version: int) -> dict[tuple[NodeKind, str], GraphNode]:
        rows = (
            await self._session.execute(
                select(GraphNode).where(
                    GraphNode.project_id == self._project_id,
                    GraphNode.graph_version == version,
                )
            )
        ).scalars()
        return {(node.kind, node.key): node for node in rows}

    async def _edge_keys(self, version: int) -> set[tuple[EdgeKind, uuid.UUID, uuid.UUID]]:
        rows = (
            await self._session.execute(
                select(GraphEdge).where(
                    GraphEdge.project_id == self._project_id,
                    GraphEdge.graph_version == version,
                )
            )
        ).scalars()
        return {(edge.kind, edge.source_node_id, edge.target_node_id) for edge in rows}

    # --- reading ---

    async def nodes(
        self, version: int, *, kind: NodeKind | None = None
    ) -> list[GraphNode]:
        query = select(GraphNode).where(
            GraphNode.project_id == self._project_id,
            GraphNode.graph_version == version,
        )
        if kind is not None:
            query = query.where(GraphNode.kind == kind)
        return list((await self._session.execute(query.order_by(GraphNode.key))).scalars())

    async def edges(self, version: int, *, kind: EdgeKind | None = None) -> list[GraphEdge]:
        query = select(GraphEdge).where(
            GraphEdge.project_id == self._project_id,
            GraphEdge.graph_version == version,
        )
        if kind is not None:
            query = query.where(GraphEdge.kind == kind)
        return list((await self._session.execute(query)).scalars())

    async def providers(self, version: int) -> list[GraphNode]:
        return await self.nodes(version, kind=NodeKind.PROVIDER)

    async def call_sites_for_provider(self, version: int, provider_key: str) -> list[GraphNode]:
        """Every call site targeting one provider."""
        provider = await self.node(version, NodeKind.PROVIDER, provider_key)
        if provider is None:
            return []

        edges = await self.edges(version, kind=EdgeKind.CALLS_PROVIDER)
        source_ids = {e.source_node_id for e in edges if e.target_node_id == provider.id}
        if not source_ids:
            return []

        rows = (
            await self._session.execute(
                select(GraphNode).where(GraphNode.id.in_(source_ids))
            )
        ).scalars()
        return sorted(rows, key=lambda node: node.key)

    async def webhook_handlers_for_provider(
        self, version: int, provider_key: str
    ) -> list[GraphNode]:
        """Symbols handling this provider's inbound webhooks."""
        provider = await self.node(version, NodeKind.PROVIDER, provider_key)
        if provider is None:
            return []

        edges = await self.edges(version, kind=EdgeKind.HANDLES_WEBHOOK_EVENT)
        source_ids = {e.source_node_id for e in edges if e.target_node_id == provider.id}
        if not source_ids:
            return []

        rows = (
            await self._session.execute(
                select(GraphNode).where(GraphNode.id.in_(source_ids))
            )
        ).scalars()
        return sorted(rows, key=lambda node: node.key)

    async def workflows_touching(
        self, version: int, node_ids: set[uuid.UUID]
    ) -> list[GraphNode]:
        return await self._reachable_of_kind(version, node_ids, NodeKind.WORKFLOW)

    async def tests_covering(self, version: int, node_ids: set[uuid.UUID]) -> list[GraphNode]:
        return await self._reachable_of_kind(version, node_ids, NodeKind.TEST)

    async def permissions_for_provider(self, version: int, provider_key: str) -> list[GraphNode]:
        provider = await self.node(version, NodeKind.PROVIDER, provider_key)
        if provider is None:
            return []
        return await self._reachable_of_kind(version, {provider.id}, NodeKind.PERMISSION)

    async def node(self, version: int, kind: NodeKind, key: str) -> GraphNode | None:
        return (
            await self._session.execute(
                select(GraphNode).where(
                    GraphNode.project_id == self._project_id,
                    GraphNode.graph_version == version,
                    GraphNode.kind == kind,
                    GraphNode.key == key,
                )
            )
        ).scalar_one_or_none()

    # --- blast radius ---

    async def blast_radius(
        self, version: int, call_site_ids: set[uuid.UUID]
    ) -> BlastRadius:
        """Everything downstream of a set of call sites.

        This is the query the graph exists for. Given the call sites a provider
        change touches, it returns the transitive closure of affected symbols,
        files, workflows, tests, and permissions — which is what turns "an
        endpoint changed" into "Checkout and Subscription Renewal are affected,
        and these three tests cover them".

        `COVERED_BY_TEST` is traversed **forwards**, because the extractor writes
        it SYMBOL → TEST. Getting this backwards is not a theoretical risk: an
        earlier version did, and every blast radius silently reported no tests.
        See `_FORWARD_EDGES` above.
        """
        reachable = await self._reachable(version, call_site_ids)
        by_kind: dict[NodeKind, list[GraphNode]] = defaultdict(list)
        for node in reachable:
            by_kind[node.kind].append(node)

        for nodes in by_kind.values():
            nodes.sort(key=lambda node: node.key)

        return BlastRadius(
            call_site_ids=set(call_site_ids),
            symbols=by_kind[NodeKind.SYMBOL],
            files=by_kind[NodeKind.FILE],
            workflows=by_kind[NodeKind.WORKFLOW],
            tests=by_kind[NodeKind.TEST],
            permissions=by_kind[NodeKind.PERMISSION],
        )

    async def _reachable(self, version: int, seeds: set[uuid.UUID]) -> list[GraphNode]:
        """Breadth-first closure over traversable edges, excluding the seeds."""
        edges = await self.edges(version)

        outgoing: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for edge in edges:
            if edge.kind in _FORWARD_EDGES:
                outgoing[edge.source_node_id].append(edge.target_node_id)
            elif edge.kind in _REVERSE_EDGES:
                outgoing[edge.target_node_id].append(edge.source_node_id)

        seen: set[uuid.UUID] = set()
        queue = deque(seeds)
        while queue:
            for neighbour in outgoing.get(queue.popleft(), ()):
                if neighbour not in seen and neighbour not in seeds:
                    seen.add(neighbour)
                    queue.append(neighbour)

        if not seen:
            return []

        rows = (
            await self._session.execute(select(GraphNode).where(GraphNode.id.in_(seen)))
        ).scalars()
        return list(rows)

    async def _reachable_of_kind(
        self, version: int, seeds: set[uuid.UUID], kind: NodeKind
    ) -> list[GraphNode]:
        reachable = await self._reachable(version, seeds)
        return sorted((n for n in reachable if n.kind is kind), key=lambda node: node.key)


@dataclass(frozen=True, slots=True)
class BlastRadius:
    """What a change to a set of call sites reaches."""

    call_site_ids: set[uuid.UUID]
    symbols: list[GraphNode]
    files: list[GraphNode]
    workflows: list[GraphNode]
    tests: list[GraphNode]
    permissions: list[GraphNode]

    @property
    def is_empty(self) -> bool:
        return not (self.symbols or self.files or self.workflows or self.tests)

    def summary(self) -> dict[str, list[str]]:
        """Compact, JSON-safe form for evidence reports and the UI."""
        return {
            "symbols": [n.label for n in self.symbols],
            "files": [n.key for n in self.files],
            "workflows": [n.label for n in self.workflows],
            "tests": [n.key for n in self.tests],
            "permissions": [n.label for n in self.permissions],
        }
