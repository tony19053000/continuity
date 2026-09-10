"""Integration mapping: deterministic extraction plus one layer of judgment.

The pipeline this module owns:

    index → deterministic extraction (CONFIRMED)
          → Integration Mapper agent (INFERRED)
          → merged graph delta → persisted version

The split is the point. Static analysis can prove that `create_payment()` calls
`acmepay` at `payment_service.py:12`. It cannot say that this is the *Checkout*
workflow — that requires reading the code the way a person would. So the model
does exactly that one job, over a bounded, secret-filtered slice of context, and
everything it contributes is marked `INFERRED` and can never overwrite a
confirmed fact.

If the agent fails or is unconfigured, mapping still completes with the
confirmed half. A graph with facts and no workflow names is useful; a graph with
invented facts is not.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base import AgentOutputInvalid
from backend.agents.contracts import IntegrationMapperInput, IntegrationMapperOutput
from backend.agents.specialists import IntegrationMapperAgent
from backend.integrations.extraction import extract
from backend.integrations.graph import (
    EdgeSpec,
    GraphDelta,
    IntegrationGraph,
    NodeSpec,
)
from backend.models.enums import Confidence, EdgeKind, EvidenceKind, NodeKind
from backend.models.schemas import Evidence
from backend.observability.logging import get_logger
from backend.repository.indexer import RepositoryIndex
from backend.repository.retrieval import ContextRetriever
from backend.repository.source import RepositorySource
from backend.shared.model_provider import ModelProvider

logger = get_logger(__name__)

#: How many call-site slices the agent sees. Bounded because the agent needs
#: enough code to name a workflow, not the repository.
MAX_CONTEXT_SLICES = 12


@dataclass(frozen=True, slots=True)
class MappingResult:
    graph_version: int
    confirmed_nodes: int
    confirmed_edges: int
    inferred_workflows: int
    agent_skipped_reason: str | None = None


def _summarize_files(index: RepositoryIndex, limit: int = 40) -> list[str]:
    """One line per source file: path, language, and its symbol names."""
    lines: list[str] = []
    for file in sorted(index.source_files(), key=lambda f: f.path)[:limit]:
        symbols = ", ".join(s.qualified_name for s in file.symbols[:8]) or "no symbols"
        lines.append(f"{file.path} ({file.language.value}): {symbols}")
    return lines


def _summarize_call_sites(delta: GraphDelta, limit: int = 40) -> list[str]:
    """One line per confirmed provider call site, with its resource."""
    lines: list[str] = []
    for node in delta.nodes:
        if node.kind is not NodeKind.CALL_SITE:
            continue
        attributes = node.attributes
        resource = attributes.get("resource") or "unknown resource"
        enclosing = attributes.get("enclosing_symbol") or "module level"
        lines.append(
            f"{node.key} — calls {attributes.get('callee')} on {resource}, "
            f"inside {enclosing}"
        )
        if len(lines) >= limit:
            break
    return lines


def _workflow_delta(output: IntegrationMapperOutput) -> GraphDelta:
    """Turn agent output into graph specs, all marked INFERRED.

    The confidence is fixed here rather than taken from the agent. An agent that
    could declare its own output confirmed would erase the distinction the whole
    design rests on.
    """
    nodes: list[NodeSpec] = []
    edges: list[EdgeSpec] = []

    for workflow in output.workflows:
        # Assembled here, not by the agent: confidence is fixed to INFERRED, and
        # `kind` is SOURCE because a workflow claim is always grounded in code.
        evidence = Evidence(
            kind=EvidenceKind.SOURCE,
            confidence=Confidence.INFERRED,
            file_path=workflow.evidence_file,
            line_start=workflow.evidence_line_start,
            line_end=max(workflow.evidence_line_end, workflow.evidence_line_start),
        )

        nodes.append(
            NodeSpec(
                kind=NodeKind.WORKFLOW,
                key=workflow.name,
                label=workflow.name,
                confidence=Confidence.INFERRED,
                evidence=evidence,
                attributes={"basis": workflow.basis},
            )
        )
        for symbol_key in workflow.symbol_keys:
            edges.append(
                EdgeSpec(
                    kind=EdgeKind.IMPLEMENTS_WORKFLOW,
                    source=(NodeKind.SYMBOL, symbol_key),
                    target=(NodeKind.WORKFLOW, workflow.name),
                    confidence=Confidence.INFERRED,
                    evidence=evidence,
                )
            )

    return GraphDelta(nodes=nodes, edges=edges)


def _drop_unresolvable_edges(delta: GraphDelta, known: set[tuple[NodeKind, str]]) -> GraphDelta:
    """Discard inferred edges whose endpoints do not exist.

    A model naming a symbol that is not in the graph has hallucinated a key.
    Dropping the edge is right: the workflow it proposed may still be correct,
    and failing the whole mapping over one bad reference would lose that.
    """
    resolvable = []
    for edge in delta.edges:
        if edge.source in known and edge.target in known:
            resolvable.append(edge)
        else:
            missing = edge.source if edge.source not in known else edge.target
            logger.warning(
                "continuity.inferred_edge_dropped",
                extra={"edge_kind": edge.kind.value, "missing_node": str(missing)},
            )
    return GraphDelta(nodes=delta.nodes, edges=resolvable)


async def map_integrations(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    index: RepositoryIndex,
    source: RepositorySource,
    model_provider: ModelProvider | None = None,
    context_budget_bytes: int = 60_000,
) -> MappingResult:
    """Build and persist one graph version for a project."""
    confirmed = extract(index)

    graph = IntegrationGraph(session, project_id)
    version = await graph.next_version()
    await graph.apply(confirmed, version=version)

    inferred_count = 0
    skipped: str | None = None

    if model_provider is None:
        skipped = "no model provider configured"
    else:
        try:
            output = await _run_mapper(
                model_provider, index, source, confirmed, project_id, context_budget_bytes
            )
        except AgentOutputInvalid as exc:
            # The confirmed half is already persisted. Losing workflow names is
            # a degraded result; losing the facts would be a failed scan.
            skipped = f"agent did not return valid output: {exc.code}"
            logger.warning(
                "continuity.integration_mapper_failed",
                extra={"project_id": str(project_id), "reason": skipped},
            )
        else:
            known = {(node.kind, node.key) for node in confirmed.nodes}
            delta = _drop_unresolvable_edges(_workflow_delta(output), known | {
                (NodeKind.WORKFLOW, workflow.name) for workflow in output.workflows
            })
            await graph.apply(delta, version=version)
            inferred_count = len(delta.nodes)

    result = MappingResult(
        graph_version=version,
        confirmed_nodes=len(confirmed.nodes),
        confirmed_edges=len(confirmed.edges),
        inferred_workflows=inferred_count,
        agent_skipped_reason=skipped,
    )
    logger.info("continuity.integration_mapping_complete", extra=asdict(result))
    return result


async def _run_mapper(
    model_provider: ModelProvider,
    index: RepositoryIndex,
    source: RepositorySource,
    confirmed: GraphDelta,
    project_id: uuid.UUID,
    context_budget_bytes: int,
) -> IntegrationMapperOutput:
    """Invoke the agent over bounded, secret-filtered context."""
    providers = [
        node.key for node in confirmed.nodes if node.kind is NodeKind.PROVIDER
    ]

    retriever = ContextRetriever(index, source, budget_bytes=context_budget_bytes)
    slices: list[str] = []
    for provider_id in providers[:4]:
        retrieved = retriever.for_call_sites(provider_id, limit=MAX_CONTEXT_SLICES)
        slices.extend(piece.render() for piece in retrieved.slices)

    agent = IntegrationMapperAgent(model_provider)
    task = IntegrationMapperInput(
        project_id=str(project_id),
        detected_providers=providers,
        file_summaries=_summarize_files(index),
        call_site_summaries=_summarize_call_sites(confirmed) + slices,
    )
    return await agent.run(task)
