"""Provider baseline: recording what "normal" is.

Monitoring is meaningless without a baseline. "Provider is at v2" is only a
change if Continuity knows this project was working against v1 — so the baseline
is what turns a version number into a *change event*.

Every baseline row carries `Evidence` pointing at the call site or manifest the
version was read from, because "you are on v1" is a claim the developer is
entitled to check.

A project with no detected integrations reaches `MONITORING_ACTIVE` with an
empty baseline. That is a valid answer, not an error: plenty of repositories
genuinely have no external providers, and treating that as a failure would
punish them for it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.graph import IntegrationGraph
from backend.models import (
    ActivityEventKind,
    Confidence,
    GraphNode,
    Integration,
    Project,
    ProviderBaseline,
    RunState,
)
from backend.models.schemas import TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import transition

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    provider_id: str
    version: str | None
    call_sites: int
    workflows: list[str]
    tests: list[str]
    permissions: list[str]


@dataclass(frozen=True, slots=True)
class BaselineResult:
    project_id: uuid.UUID
    graph_version: int
    entries: list[BaselineEntry]

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def summary(self) -> dict[str, object]:
        return {
            "graph_version": self.graph_version,
            "providers": len(self.entries),
            "integration_points": sum(entry.call_sites for entry in self.entries),
            "workflows": sorted({w for entry in self.entries for w in entry.workflows}),
            "versions": {
                entry.provider_id: entry.version
                for entry in self.entries
                if entry.version
            },
        }


async def establish_baseline(
    session: AsyncSession, project: Project, *, graph_version: int
) -> BaselineResult:
    """Record the current provider baseline and begin monitoring.

    Produces the chain the product is built on:

        Provider → files → functions → workflows → tests → permissions

    and then moves the project to `MONITORING_ACTIVE`.
    """
    graph = IntegrationGraph(session, project.id)
    providers = await graph.providers(graph_version)
    entries: list[BaselineEntry] = []

    for provider in providers:
        call_sites = await graph.call_sites_for_provider(graph_version, provider.key)
        # Webhook handlers reach the provider through HANDLES_WEBHOOK_EVENT, not
        # through a call site, so seeding from call sites alone would omit every
        # workflow that only receives events — exactly the case a renamed
        # webhook event breaks.
        handlers = await graph.webhook_handlers_for_provider(graph_version, provider.key)
        seeds = {node.id for node in call_sites} | {node.id for node in handlers}
        radius = await graph.blast_radius(graph_version, seeds)
        permissions = await graph.permissions_for_provider(graph_version, provider.key)

        attributes = provider.attributes or {}
        version = attributes.get("detected_api_version")

        entries.append(
            BaselineEntry(
                provider_id=provider.key,
                version=str(version) if version else None,
                call_sites=len(call_sites),
                workflows=[node.label for node in radius.workflows],
                tests=[node.key for node in radius.tests],
                permissions=[node.label for node in permissions],
            )
        )

        await _upsert_integration(session, project, provider, len(call_sites))
        await _upsert_baseline(session, project, provider.key, version, provider.evidence)

        await events.emit(
            session,
            kind=ActivityEventKind.INTEGRATION_DETECTED,
            actor="system",
            summary=(
                f"{provider.label}: {len(call_sites)} integration point(s)"
                + (f", API {version}" if version else "")
            ),
            project_id=project.id,
        )

    project.current_graph_version = graph_version
    result = BaselineResult(
        project_id=project.id, graph_version=graph_version, entries=entries
    )

    await events.emit(
        session,
        kind=ActivityEventKind.INTEGRATION_MAPPING_COMPLETE,
        actor="system",
        summary=(
            f"Mapped {len(entries)} provider(s) across "
            f"{sum(e.call_sites for e in entries)} integration point(s)."
            if entries
            else "No external integrations detected."
        ),
        project_id=project.id,
    )

    await _advance_to_monitoring(session, project, result)
    return result


async def _advance_to_monitoring(
    session: AsyncSession, project: Project, result: BaselineResult
) -> None:
    """Walk the documented states through to `MONITORING_ACTIVE`.

    Each hop is a real recorded transition rather than a jump, so the run
    timeline shows what happened instead of a single leap to the end state.
    """
    path = [
        RunState.INTEGRATION_MAPPING_RUNNING,
        RunState.INTEGRATION_MAPPING_COMPLETE,
        RunState.MONITORING_ACTIVE,
    ]
    reasons = [
        "Integration mapping started.",
        f"Mapped {len(result.entries)} provider(s).",
        "Baseline established; monitoring active.",
    ]

    for to_state, reason in zip(path, reasons, strict=True):
        if project.state is to_state:
            continue
        await transition(
            session,
            from_state=project.state,
            to_state=to_state,
            evidence=TransitionEvidence(
                reason=reason, actor="system", detail=result.summary()
            ),
            project_id=project.id,
        )
        project.state = to_state
        await session.flush()


async def _upsert_integration(
    session: AsyncSession, project: Project, provider: GraphNode, call_sites: int
) -> None:
    """Maintain the per-provider summary the Integrations page reads."""
    attributes = provider.attributes or {}

    existing = (
        await session.execute(
            select(Integration).where(
                Integration.project_id == project.id,
                Integration.provider_id == provider.key,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = Integration(project_id=project.id, provider_id=provider.key)
        session.add(existing)

    existing.display_name = provider.label
    existing.sdk_package = str(attributes["package"]) if attributes.get("package") else None
    existing.detected_api_version = (
        str(attributes["detected_api_version"])
        if attributes.get("detected_api_version")
        else None
    )
    # Everything the extractor produces is deterministic, so a provider row is
    # confirmed regardless of what the mapper agent added around it.
    existing.confidence = Confidence.CONFIRMED
    existing.evidence = provider.evidence
    existing.integration_points = call_sites
    await session.flush()


async def _upsert_baseline(
    session: AsyncSession,
    project: Project,
    provider_id: str,
    version: object,
    evidence: dict[str, object] | None,
) -> None:
    existing = (
        await session.execute(
            select(ProviderBaseline).where(
                ProviderBaseline.project_id == project.id,
                ProviderBaseline.provider_id == provider_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = ProviderBaseline(
            project_id=project.id,
            provider_id=provider_id,
            version=str(version) if version else "unknown",
            established_at=datetime.now(UTC),
        )
        session.add(existing)
    else:
        existing.version = str(version) if version else "unknown"
        existing.established_at = datetime.now(UTC)

    existing.evidence = evidence
    await session.flush()


def describe_chain(result: BaselineResult) -> str:
    """Human-readable `Provider → … → permissions` chain, for logs and reports."""
    if result.is_empty:
        return "No external integrations detected."

    lines: list[str] = []
    for entry in result.entries:
        lines.append(f"{entry.provider_id} (API {entry.version or 'unknown'})")
        lines.append(f"  integration points : {entry.call_sites}")
        lines.append(f"  workflows          : {', '.join(entry.workflows) or 'none inferred'}")
        lines.append(f"  tests              : {', '.join(entry.tests) or 'none'}")
        lines.append(f"  permissions        : {', '.join(entry.permissions) or 'none detected'}")
    return "\n".join(lines)
