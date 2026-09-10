"""C4-01 acceptance: the graph persists every kind and answers every query."""

from __future__ import annotations

import uuid

import pytest

from backend.integrations.graph import (
    ConfirmedNodeOverwrite,
    EdgeSpec,
    GraphDelta,
    IntegrationGraph,
    NodeSpec,
)
from backend.models import (
    Confidence,
    EdgeKind,
    EvidenceKind,
    NodeKind,
    Project,
    Repository,
    User,
)
from backend.models.schemas import Evidence
from backend.models.session import session_scope


def _evidence(path: str = "app/x.py", line: int = 1) -> Evidence:
    return Evidence(
        kind=EvidenceKind.SOURCE,
        confidence=Confidence.CONFIRMED,
        file_path=path,
        line_start=line,
        line_end=line,
    )


async def _make_project(session) -> Project:
    user = User(google_subject=f"s-{uuid.uuid4()}", email="g@example.test")
    session.add(user)
    await session.flush()
    repo = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
    session.add(repo)
    await session.flush()
    project = Project(user_id=user.id, repository_id=repo.id, name="p")
    session.add(project)
    await session.flush()
    return project


def _full_graph() -> GraphDelta:
    """A graph exercising all eight node kinds and all seven edge kinds.

    Shaped like a real project so `blast_radius` has something meaningful to
    traverse: a provider, its SDK, a file with two symbols, a call site inside
    one of them, two workflows, a test, and a permission.
    """
    nodes = [
        NodeSpec(NodeKind.PROVIDER, "acmepay", "acmepay", Confidence.CONFIRMED, _evidence()),
        NodeSpec(NodeKind.SDK, "acmepay==2.1", "acmepay", Confidence.CONFIRMED, _evidence()),
        NodeSpec(NodeKind.FILE, "app/pay.py", "app/pay.py", Confidence.CONFIRMED, _evidence()),
        NodeSpec(
            NodeKind.SYMBOL, "app/pay.py::create", "create()", Confidence.CONFIRMED, _evidence()
        ),
        NodeSpec(
            NodeKind.SYMBOL, "app/pay.py::hook", "hook()", Confidence.CONFIRMED, _evidence()
        ),
        NodeSpec(
            NodeKind.CALL_SITE, "app/pay.py:12:post", "post", Confidence.CONFIRMED, _evidence()
        ),
        NodeSpec(NodeKind.WORKFLOW, "Checkout", "Checkout", Confidence.INFERRED, _evidence()),
        NodeSpec(NodeKind.WORKFLOW, "Events", "Events", Confidence.INFERRED, _evidence()),
        NodeSpec(
            NodeKind.TEST, "tests/test_pay.py", "tests/test_pay.py", Confidence.CONFIRMED,
            _evidence(),
        ),
        NodeSpec(
            NodeKind.PERMISSION, "acmepay:customers.read", "customers.read",
            Confidence.CONFIRMED, _evidence(),
        ),
    ]
    edges = [
        EdgeSpec(EdgeKind.DECLARES_SDK, (NodeKind.PROVIDER, "acmepay"),
                 (NodeKind.SDK, "acmepay==2.1"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.DEFINED_IN, (NodeKind.SYMBOL, "app/pay.py::create"),
                 (NodeKind.FILE, "app/pay.py"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.CALLS_PROVIDER, (NodeKind.CALL_SITE, "app/pay.py:12:post"),
                 (NodeKind.PROVIDER, "acmepay"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.DEFINED_IN, (NodeKind.CALL_SITE, "app/pay.py:12:post"),
                 (NodeKind.SYMBOL, "app/pay.py::create"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.IMPLEMENTS_WORKFLOW, (NodeKind.SYMBOL, "app/pay.py::create"),
                 (NodeKind.WORKFLOW, "Checkout"), Confidence.INFERRED, _evidence()),
        EdgeSpec(EdgeKind.IMPLEMENTS_WORKFLOW, (NodeKind.SYMBOL, "app/pay.py::hook"),
                 (NodeKind.WORKFLOW, "Events"), Confidence.INFERRED, _evidence()),
        EdgeSpec(EdgeKind.COVERED_BY_TEST, (NodeKind.SYMBOL, "app/pay.py::create"),
                 (NodeKind.TEST, "tests/test_pay.py"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.REQUIRES_PERMISSION, (NodeKind.PROVIDER, "acmepay"),
                 (NodeKind.PERMISSION, "acmepay:customers.read"), Confidence.CONFIRMED,
                 _evidence()),
        EdgeSpec(EdgeKind.HANDLES_WEBHOOK_EVENT, (NodeKind.SYMBOL, "app/pay.py::hook"),
                 (NodeKind.PROVIDER, "acmepay"), Confidence.CONFIRMED, _evidence()),
    ]
    return GraphDelta(nodes=nodes, edges=edges)


# --- Persistence ---------------------------------------------------------


async def test_every_node_and_edge_kind_persists_with_evidence(database: None) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)
        await graph.apply(_full_graph(), version=1)

        nodes = await graph.nodes(1)
        edges = await graph.edges(1)

    assert {node.kind for node in nodes} == set(NodeKind), "not every node kind persisted"
    assert {edge.kind for edge in edges} == set(EdgeKind), "not every edge kind persisted"
    assert all(node.evidence is not None for node in nodes)
    assert all(edge.evidence is not None for edge in edges)
    assert all(node.confidence in (Confidence.CONFIRMED, Confidence.INFERRED) for node in nodes)


async def test_versions_are_independent(database: None) -> None:
    """A new scan must not disturb the version a migration is reading."""
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)

        await graph.apply(_full_graph(), version=1)
        assert await graph.next_version() == 2

        await graph.apply(
            GraphDelta(
                nodes=[
                    NodeSpec(NodeKind.PROVIDER, "other", "other", Confidence.CONFIRMED,
                             _evidence())
                ]
            ),
            version=2,
        )

        assert len(await graph.providers(1)) == 1
        assert (await graph.providers(1))[0].key == "acmepay"
        assert (await graph.providers(2))[0].key == "other"


async def test_reapplying_a_node_updates_rather_than_duplicates(database: None) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)

        spec = NodeSpec(NodeKind.PROVIDER, "acmepay", "old", Confidence.CONFIRMED, _evidence())
        await graph.apply(GraphDelta(nodes=[spec]), version=1)
        await graph.apply(
            GraphDelta(
                nodes=[
                    NodeSpec(NodeKind.PROVIDER, "acmepay", "new", Confidence.CONFIRMED,
                             _evidence())
                ]
            ),
            version=1,
        )

        providers = await graph.providers(1)

    assert len(providers) == 1
    assert providers[0].label == "new"


async def test_an_inferred_node_cannot_overwrite_a_confirmed_one(database: None) -> None:
    """The rule the whole confirmed/inferred split rests on.

    A model contradicting deterministic analysis raises rather than winning
    silently — a disagreement worth surfacing, not a no-op.
    """
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)

        await graph.apply(
            GraphDelta(
                nodes=[
                    NodeSpec(NodeKind.PROVIDER, "acmepay", "confirmed", Confidence.CONFIRMED,
                             _evidence())
                ]
            ),
            version=1,
        )

        with pytest.raises(ConfirmedNodeOverwrite):
            await graph.apply(
                GraphDelta(
                    nodes=[
                        NodeSpec(NodeKind.PROVIDER, "acmepay", "hallucinated",
                                 Confidence.INFERRED, _evidence())
                    ]
                ),
                version=1,
            )


async def test_a_dangling_edge_is_refused(database: None) -> None:
    """A producer bug should fail loudly, not persist a puzzle."""
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)

        with pytest.raises(ValueError, match="missing node"):
            await graph.apply(
                GraphDelta(
                    nodes=[
                        NodeSpec(NodeKind.PROVIDER, "p", "p", Confidence.CONFIRMED, _evidence())
                    ],
                    edges=[
                        EdgeSpec(EdgeKind.CALLS_PROVIDER, (NodeKind.CALL_SITE, "nope"),
                                 (NodeKind.PROVIDER, "p"), Confidence.CONFIRMED, _evidence())
                    ],
                ),
                version=1,
            )


async def test_evidence_excerpts_are_secret_filtered_before_storage(database: None) -> None:
    from tests.support.secret_samples import GITHUB_TOKEN

    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)

        await graph.apply(
            GraphDelta(
                nodes=[
                    NodeSpec(
                        NodeKind.FILE, "app/x.py", "app/x.py", Confidence.CONFIRMED,
                        Evidence(
                            kind=EvidenceKind.SOURCE,
                            confidence=Confidence.CONFIRMED,
                            file_path="app/x.py",
                            excerpt=f'TOKEN = "{GITHUB_TOKEN}"',
                        ),
                    )
                ]
            ),
            version=1,
        )
        stored = await graph.node(1, NodeKind.FILE, "app/x.py")

    assert stored is not None
    assert GITHUB_TOKEN not in str(stored.evidence)
    assert "[REDACTED]" in str(stored.evidence)


# --- Queries -------------------------------------------------------------


async def test_query_methods_return_the_expected_nodes(database: None) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)
        await graph.apply(_full_graph(), version=1)

        assert [n.key for n in await graph.providers(1)] == ["acmepay"]
        assert [n.key for n in await graph.call_sites_for_provider(1, "acmepay")] == [
            "app/pay.py:12:post"
        ]
        assert [n.label for n in await graph.permissions_for_provider(1, "acmepay")] == [
            "customers.read"
        ]
        assert [n.key for n in await graph.webhook_handlers_for_provider(1, "acmepay")] == [
            "app/pay.py::hook"
        ]


async def test_queries_for_an_unknown_provider_return_empty(database: None) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)
        await graph.apply(_full_graph(), version=1)

        assert await graph.call_sites_for_provider(1, "nope") == []
        assert await graph.permissions_for_provider(1, "nope") == []


# --- Blast radius --------------------------------------------------------


async def test_blast_radius_matches_the_hand_computed_closure(database: None) -> None:
    """The query the graph exists for, checked against a set worked out by hand.

    Seeding from the single call site, the closure is:
      call site -> create()          (DEFINED_IN)
      create()  -> app/pay.py        (DEFINED_IN)
      create()  -> Checkout          (IMPLEMENTS_WORKFLOW)
      create()  -> tests/test_pay.py (COVERED_BY_TEST)
      call site -> acmepay           (CALLS_PROVIDER)
      acmepay   -> customers.read    (REQUIRES_PERMISSION)

    `hook()` and `Events` are NOT reachable: nothing links the call site to the
    webhook handler, and that is correct — a change to this call site does not
    touch the event handler.
    """
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)
        await graph.apply(_full_graph(), version=1)

        call_site = await graph.node(1, NodeKind.CALL_SITE, "app/pay.py:12:post")
        assert call_site is not None
        radius = await graph.blast_radius(1, {call_site.id})

    assert [n.key for n in radius.symbols] == ["app/pay.py::create"]
    assert [n.key for n in radius.files] == ["app/pay.py"]
    assert [n.key for n in radius.workflows] == ["Checkout"]
    assert [n.key for n in radius.tests] == ["tests/test_pay.py"]
    assert [n.label for n in radius.permissions] == ["customers.read"]
    assert not radius.is_empty


async def test_blast_radius_reaches_tests(database: None) -> None:
    """Regression: COVERED_BY_TEST was traversed in the wrong direction.

    The query returned no tests for every blast radius — it looked like it
    worked and quietly answered "nothing covers this", which would have made
    every migration skip the tests that actually matter.
    """
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)
        await graph.apply(_full_graph(), version=1)

        symbol = await graph.node(1, NodeKind.SYMBOL, "app/pay.py::create")
        assert symbol is not None
        tests = await graph.tests_covering(1, {symbol.id})

    assert [n.key for n in tests] == ["tests/test_pay.py"]


async def test_blast_radius_of_nothing_is_empty(database: None) -> None:
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)
        await graph.apply(_full_graph(), version=1)

        radius = await graph.blast_radius(1, set())

    assert radius.is_empty
    assert radius.summary() == {
        "symbols": [], "files": [], "workflows": [], "tests": [], "permissions": []
    }


async def test_blast_radius_excludes_its_own_seeds(database: None) -> None:
    """The answer is what a change *reaches*, not the change itself."""
    async with session_scope() as session:
        project = await _make_project(session)
        graph = IntegrationGraph(session, project.id)
        await graph.apply(_full_graph(), version=1)

        call_site = await graph.node(1, NodeKind.CALL_SITE, "app/pay.py:12:post")
        assert call_site is not None
        radius = await graph.blast_radius(1, {call_site.id})

    assert call_site.id not in {n.id for n in radius.symbols}


def test_evidence_is_structurally_required_on_every_spec() -> None:
    """C4-01 promises every persisted node and edge is traceable.

    Enforced by the type rather than by convention: `evidence` has no default,
    so a caller that forgets it fails at construction instead of persisting an
    unsourced claim that surfaces in the UI as an assertion nobody can check.
    """
    import dataclasses

    for spec_type in (NodeSpec, EdgeSpec):
        evidence_field = next(
            f for f in dataclasses.fields(spec_type) if f.name == "evidence"
        )
        assert evidence_field.default is dataclasses.MISSING, (
            f"{spec_type.__name__}.evidence must not be optional"
        )
        assert evidence_field.default_factory is dataclasses.MISSING


def test_constructing_a_spec_without_evidence_fails() -> None:
    with pytest.raises(TypeError):
        NodeSpec(NodeKind.PROVIDER, "p", "p", Confidence.CONFIRMED)  # type: ignore[call-arg]
