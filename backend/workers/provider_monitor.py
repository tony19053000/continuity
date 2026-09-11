"""Provider monitoring.

Runs on a schedule, per provider, driven by adapter sources. **Nothing about it
depends on a user action or a demo trigger** — that independence is what makes
Continuity a monitoring product rather than a button, and a test asserts the
monitor completes without any inbound request.

The flow:

    adapter → current version vs baseline
            → fetch both specs (content-addressed, deduplicated)
            → deterministic diff  (C5-03)
            → Change Scout over the changelog  (C5-04)
            → deduplicated change events → CHANGE_DETECTED

Change events are deduplicated by
`(provider_id, old_version, new_version, change_type, resource)` at the database
level, so polling twice cannot manufacture a second event even under a race.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    ActivityEventKind,
    ChangeEvent,
    Integration,
    Project,
    Provider,
    RunState,
)
from backend.models.schemas import ProviderChange, TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.providers.base import (
    CapabilityNotSupported,
    ProviderAdapter,
    ProviderCapability,
    ProviderFetchFailed,
)
from backend.providers.diff import MalformedSpec, SpecDiffer
from backend.providers.registry import ProviderRegistry
from backend.providers.registry import registry as default_registry
from backend.providers.storage import baseline_version, store_document
from backend.shared.model_provider import ModelProvider

logger = get_logger(__name__)


@dataclass(slots=True)
class MonitorResult:
    """What one monitoring pass found, per provider."""

    provider_id: str
    baseline: str | None
    current: str | None
    changes_detected: int = 0
    changes_recorded: int = 0
    duplicates_skipped: int = 0
    spec_derived: int = 0
    changelog_derived: int = 0
    injection_suspected: bool = False
    #: Advisory only; see `ScoutResult.model_reported_injection`.
    model_reported_injection: bool = False
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def has_new_version(self) -> bool:
        return bool(
            self.baseline and self.current and self.baseline != self.current
        )


async def monitor_provider(
    session: AsyncSession,
    project: Project,
    provider_id: str,
    *,
    adapter_registry: ProviderRegistry | None = None,
    model_provider: ModelProvider | None = None,
) -> MonitorResult:
    """Check one provider for changes affecting one project."""
    adapters = adapter_registry or default_registry
    adapter = adapters.try_get(provider_id)

    if adapter is None:
        # Most projects depend on providers nobody has written an adapter for.
        # That is an unmonitored provider, not a failure.
        return MonitorResult(
            provider_id=provider_id,
            baseline=None,
            current=None,
            skipped_reason="no adapter registered for this provider",
        )

    baseline = await baseline_version(session, project, provider_id)
    result = MonitorResult(provider_id=provider_id, baseline=baseline, current=None)

    try:
        current = await adapter.get_current_version()
    except (CapabilityNotSupported, ProviderFetchFailed) as exc:
        result.skipped_reason = f"current version unavailable: {exc.code}"
        result.errors.append(str(exc))
        await _record_check(session, provider_id, error=str(exc))
        return result

    result.current = current.version
    await _record_check(session, provider_id, error=None)

    if baseline is None:
        result.skipped_reason = "no baseline recorded for this project yet"
        return result

    if baseline == current.version:
        return result  # unchanged; the common case

    changes = await _collect_changes(session, adapter, baseline, current.version, result)
    if model_provider is not None:
        changes.extend(
            await _scout_changelog(
                adapter, baseline, current.version, changes, model_provider, result
            )
        )

    result.changes_detected = len(changes)
    await _record_changes(session, project, changes, result)
    return result


async def _collect_changes(
    session: AsyncSession,
    adapter: ProviderAdapter,
    baseline: str,
    current: str,
    result: MonitorResult,
) -> list[ProviderChange]:
    """Deterministic spec diff, when the adapter can supply specs."""
    if ProviderCapability.OPENAPI_SPEC not in adapter.capabilities:
        return []

    from backend.providers.base import ProviderVersion

    try:
        old_document = await adapter.fetch_openapi_spec(ProviderVersion(version=baseline))
        new_document = await adapter.fetch_openapi_spec(ProviderVersion(version=current))
    except (CapabilityNotSupported, ProviderFetchFailed) as exc:
        result.errors.append(f"spec fetch failed: {exc}")
        return []

    old_stored = await store_document(session, adapter.provider_id, baseline, old_document)
    new_stored = await store_document(session, adapter.provider_id, current, new_document)

    if not (old_stored.is_usable_spec and new_stored.is_usable_spec):
        # Fail closed. A partial diff would report unreadable endpoints as
        # removed, and a migration would then "fix" code that was never broken.
        result.errors.append("one or both specs were unparseable; diff skipped")
        return []

    differ = SpecDiffer(
        provider_id=adapter.provider_id,
        old_version=baseline,
        new_version=current,
        source=new_stored.source,
    )

    try:
        changes = differ.diff(old_document.content, new_document.content)
    except MalformedSpec as exc:
        result.errors.append(f"diff failed: {exc}")
        return []

    result.spec_derived = len(changes)
    return changes


async def _scout_changelog(
    adapter: ProviderAdapter,
    baseline: str,
    current: str,
    spec_changes: list[ProviderChange],
    model_provider: ModelProvider,
    result: MonitorResult,
) -> list[ProviderChange]:
    """Ask the Change Scout for what prose reveals and the differ cannot see."""
    if ProviderCapability.CHANGELOG not in adapter.capabilities:
        return []

    from backend.agents.change_scout import scout_changelog
    from backend.providers.base import ProviderVersion

    try:
        document = await adapter.fetch_changelog(ProviderVersion(version=baseline))
    except (CapabilityNotSupported, ProviderFetchFailed) as exc:
        result.errors.append(f"changelog fetch failed: {exc}")
        return []

    try:
        scouted = await scout_changelog(
            model_provider,
            provider_id=adapter.provider_id,
            old_version=baseline,
            new_version=current,
            document=document,
            spec_changes=spec_changes,
        )
    except Exception as exc:
        # The deterministic half already succeeded. Losing prose interpretation
        # degrades the result; losing the spec diff would lose the facts.
        result.errors.append(f"change scout failed: {type(exc).__name__}")
        logger.warning(
            "continuity.change_scout_failed",
            extra={"provider_id": adapter.provider_id, "error": type(exc).__name__},
        )
        return []

    result.changelog_derived = len(scouted.changes)
    result.injection_suspected = scouted.injection_suspected
    result.model_reported_injection = scouted.model_reported_injection
    return scouted.changes


async def _record_changes(
    session: AsyncSession,
    project: Project,
    changes: list[ProviderChange],
    result: MonitorResult,
) -> None:
    """Persist change events, deduplicating, then move the project state."""
    for change in changes:
        record = ChangeEvent(
            provider_id=change.provider_id,
            old_version=change.old_version,
            new_version=change.new_version,
            change_type=change.change_type,
            resource=change.resource,
            old_contract=change.old_contract,
            new_contract=change.new_contract,
            breaking=change.breaking,
            security_relevant=change.security_relevant,
            authentication_relevant=change.authentication_relevant,
            source=change.source.model_dump(mode="json"),
            evidence=change.evidence.model_dump(mode="json"),
            detected_at=datetime.now(UTC),
        )
        try:
            # The savepoint opens *before* the row is added, so rolling it back
            # expunges the pending object too. Adding first and flushing inside
            # left the failed insert pending on the outer session, which then
            # refused every later operation with PendingRollbackError -- the
            # second poll of an unchanged provider crashed instead of
            # deduplicating, which is the exact case dedup exists for.
            async with session.begin_nested():
                session.add(record)
                await session.flush()
        except IntegrityError:
            result.duplicates_skipped += 1
            continue

        result.changes_recorded += 1

        await events.emit(
            session,
            kind=ActivityEventKind.PROVIDER_CHANGE_DETECTED,
            actor="change_scout",
            summary=(
                f"{change.provider_id} {change.old_version}→{change.new_version}: "
                f"{change.change_type.value} on {change.resource}"
                + (" (breaking)" if change.breaking else "")
            ),
            project_id=project.id,
            evidence=change.evidence,
        )

    # The caller may hand us a `Project` loaded in another session. Mutating a
    # detached instance would record the transition in the log and leave the
    # project row on its old state -- the run would look started and never be.
    tracked = await session.get(Project, project.id) or project

    if result.changes_recorded and can_transition(tracked.state, RunState.CHANGE_DETECTED):
        await transition(
            session,
            from_state=tracked.state,
            to_state=RunState.CHANGE_DETECTED,
            evidence=TransitionEvidence(
                reason=(
                    f"{result.changes_recorded} change(s) detected for "
                    f"{result.provider_id}"
                ),
                actor="provider_monitor",
                detail={
                    "provider_id": result.provider_id,
                    "spec_derived": result.spec_derived,
                    "changelog_derived": result.changelog_derived,
                },
            ),
            project_id=project.id,
        )
        tracked.state = RunState.CHANGE_DETECTED
        await session.flush()


async def _record_check(session: AsyncSession, provider_id: str, *, error: str | None) -> None:
    """Record that a provider was checked, and whether it answered.

    Surfaced as "Last checked" on the Integrations page. A provider that has
    never been checked says so rather than showing a stale timestamp.
    """
    provider = (
        await session.execute(select(Provider).where(Provider.provider_id == provider_id))
    ).scalar_one_or_none()

    if provider is None:
        provider = Provider(
            provider_id=provider_id, display_name=provider_id, adapter_name=provider_id
        )
        session.add(provider)

    provider.last_checked_at = datetime.now(UTC)
    provider.last_check_error = error
    await session.flush()


async def monitor_project(
    session: AsyncSession,
    project: Project,
    *,
    adapter_registry: ProviderRegistry | None = None,
    model_provider: ModelProvider | None = None,
) -> list[MonitorResult]:
    """Check every provider this project integrates with.

    Driven by the project's own integrations, so monitoring scope follows what
    the repository actually uses rather than a configured list someone must
    remember to update.
    """
    integrations = (
        await session.execute(
            select(Integration.provider_id).where(Integration.project_id == project.id)
        )
    ).scalars().all()

    results: list[MonitorResult] = []
    for provider_id in sorted(set(integrations)):
        results.append(
            await monitor_provider(
                session,
                project,
                provider_id,
                adapter_registry=adapter_registry,
                model_provider=model_provider,
            )
        )

    logger.info(
        "continuity.provider_monitor_pass",
        extra={
            "project_id": str(project.id),
            "providers_checked": len(results),
            "changes_recorded": sum(r.changes_recorded for r in results),
        },
    )
    return results
