"""C5-01 and C5-05: monitoring, end to end, driven by a pluggable adapter.

Two claims are under test here, and both are structural rather than incidental.

First, **pluggability**: `FixtureProviderAdapter` lives in `tests/support/` and
is registered at runtime. Driving it through the whole pipeline proves that an
externally-built adapter — Provider Lab, a demo provider, a real one — needs no
edit to any module outside `backend/providers/`.

Second, **autonomy**: nothing in this file constructs an HTTP client or touches
the FastAPI app. Monitoring is a worker function a scheduler calls, not
something a user request triggers.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.models import (
    ChangeEvent,
    Integration,
    Project,
    Provider,
    ProviderSpec,
    Repository,
    RunState,
    User,
)
from backend.models.enums import ChangeType
from backend.models.session import session_scope
from backend.providers.registry import ProviderRegistry
from backend.providers.storage import advance_baseline
from backend.workers.provider_monitor import monitor_project
from tests.support.provider_fixtures import (
    FixtureProviderAdapter,
    MinimalProviderAdapter,
)

pytestmark = pytest.mark.usefixtures("database")

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _project(
    *,
    providers: tuple[str, ...] = ("acmepay",),
    state: RunState = RunState.MONITORING_ACTIVE,
) -> Project:
    """A project that integrates with the given providers.

    Defaults to `MONITORING_ACTIVE`, because that is the state a project being
    polled is actually in — it is the only state from which `CHANGE_DETECTED`
    is reachable.
    """
    async with session_scope() as session:
        user = User(google_subject=f"sub-{uuid.uuid4()}", email="owner@example.test")
        session.add(user)
        await session.flush()

        repository = Repository(owner="acme", name=f"app-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()

        project = Project(
            user_id=user.id,
            repository_id=repository.id,
            name="commerce-api",
            state=state,
        )
        session.add(project)
        await session.flush()

        for provider_id in providers:
            session.add(
                Integration(
                    project_id=project.id,
                    provider_id=provider_id,
                    display_name=provider_id,
                )
            )
        await session.flush()
        await session.refresh(project)
        return project


def _registry(*adapters: object) -> ProviderRegistry:
    """A registry scoped to one test.

    Never the module-level singleton: a test that mutated shared registration
    would leak into every other test in the session.
    """
    registry = ProviderRegistry()
    for adapter in adapters:
        registry.register(adapter)  # type: ignore[arg-type]
    return registry


# --- C5-01: a new adapter plugs in ---------------------------------------


async def test_a_newly_registered_adapter_drives_monitoring_end_to_end() -> None:
    """C5-01 acceptance.

    The adapter is defined outside `backend/` entirely and handed to the monitor
    through the registry. If plugging in a provider required a core change, this
    test could not be written without one.
    """
    project = await _project()
    adapter = FixtureProviderAdapter()

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v1")

    async with session_scope() as session:
        (result,) = await monitor_project(
            session, project, adapter_registry=_registry(adapter)
        )

    assert result.provider_id == "acmepay"
    assert result.baseline == "v1"
    assert result.current == "v2"
    assert result.has_new_version
    assert result.skipped_reason is None
    assert result.errors == []
    # The full spec-derivable change set from the fixture pair.
    assert result.spec_derived == 14
    assert result.changes_recorded == 14
    assert adapter.fetch_count == 2


def test_the_fixture_adapter_depends_only_on_the_provider_surface() -> None:
    """The pluggability claim, asserted rather than asserted-about.

    An adapter that reached into `backend.workers` or `backend.integrations`
    would still pass the end-to-end test above while quietly proving nothing —
    it would be coupled to the pipeline, not plugged into it. This restricts the
    fixture to the public provider surface plus shared enums.
    """
    source = (REPO_ROOT / "tests" / "support" / "provider_fixtures.py").read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    backend_imports = {m for m in imported if m.startswith("backend.")}

    allowed = {"backend.providers.base", "backend.models.enums"}
    assert backend_imports <= allowed, (
        f"the fixture adapter reaches beyond the provider surface: "
        f"{sorted(backend_imports - allowed)}"
    )


async def test_a_provider_with_no_adapter_is_unmonitored_not_failed() -> None:
    """Most projects depend on providers nobody has written an adapter for.

    Reporting that as an error would bury the real ones; reporting it as success
    would claim monitoring that is not happening. It is neither.
    """
    project = await _project(providers=("acmepay", "stripe"))

    async with session_scope() as session:
        results = await monitor_project(
            session, project, adapter_registry=_registry(FixtureProviderAdapter())
        )

    by_id = {r.provider_id: r for r in results}
    assert sorted(by_id) == ["acmepay", "stripe"]
    assert by_id["stripe"].skipped_reason == "no adapter registered for this provider"
    assert by_id["stripe"].errors == []
    assert by_id["stripe"].changes_recorded == 0


async def test_an_adapter_without_the_spec_capability_yields_no_spec_diff() -> None:
    """Capability gating reaching the pipeline.

    `MinimalProviderAdapter` can report a version and nothing else. The monitor
    must record the check and produce no changes — not crash, and not invent a
    diff from data it never had.
    """
    project = await _project(providers=("minimalpay",))

    async with session_scope() as session:
        await advance_baseline(session, project, "minimalpay", "1999-01-01")

    async with session_scope() as session:
        (result,) = await monitor_project(
            session, project, adapter_registry=_registry(MinimalProviderAdapter())
        )

    assert result.current == "2024-01-01"
    assert result.has_new_version
    assert result.spec_derived == 0
    assert result.changes_recorded == 0
    assert result.errors == []


# --- C5-05: deduplication ------------------------------------------------


async def test_polling_the_same_version_repeatedly_produces_one_change_event() -> None:
    """C5-05 acceptance, stated exactly as the ticket does.

    This is the property that makes autonomous polling safe to run on a short
    interval. Without it, every poll would re-detect the same release and the
    pipeline would open a migration run per tick.
    """
    project = await _project()
    adapter = FixtureProviderAdapter()

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v1")

    passes = []
    for _ in range(3):
        async with session_scope() as session:
            (result,) = await monitor_project(
                session, project, adapter_registry=_registry(adapter)
            )
            passes.append(result)

    async with session_scope() as session:
        events = (
            await session.execute(
                select(func.count()).select_from(ChangeEvent).where(
                    ChangeEvent.provider_id == "acmepay"
                )
            )
        ).scalar_one()
        specs = (
            await session.execute(
                select(func.count()).select_from(ProviderSpec).where(
                    ProviderSpec.provider_id == "acmepay"
                )
            )
        ).scalar_one()

    # Every pass detects the same 14 changes...
    assert [p.changes_detected for p in passes] == [14, 14, 14]
    # ...but only the first records them.
    assert [p.changes_recorded for p in passes] == [14, 0, 0]
    assert [p.duplicates_skipped for p in passes] == [0, 14, 14]
    assert events == 14
    # Two documents, refetched six times: content addressing, not version labels.
    assert specs == 2
    assert adapter.fetch_count == 6


async def test_the_dedup_key_is_per_resource_not_per_provider() -> None:
    """Two changes of the same type on different endpoints are two events.

    Deduplicating on `(provider, versions, type)` alone would collapse them and
    lose one — the fixture removes two distinct endpoints.
    """
    project = await _project()

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v1")

    async with session_scope() as session:
        await monitor_project(
            session, project, adapter_registry=_registry(FixtureProviderAdapter())
        )

    async with session_scope() as session:
        resources = (
            await session.execute(
                select(ChangeEvent.resource).where(
                    ChangeEvent.provider_id == "acmepay",
                    ChangeEvent.change_type == ChangeType.ENDPOINT_ADDED,
                )
            )
        ).scalars().all()

    assert sorted(resources) == ["GET /v2/disputes"]


async def test_an_unchanged_provider_records_the_check_and_nothing_else() -> None:
    """The common case: the provider has not moved.

    It must still be visible as checked — "last checked 6 minutes ago" is what
    distinguishes a stable provider from one nobody is watching.
    """
    project = await _project()

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v2")

    async with session_scope() as session:
        (result,) = await monitor_project(
            session, project, adapter_registry=_registry(FixtureProviderAdapter())
        )

    assert result.has_new_version is False
    assert result.changes_detected == 0
    assert result.changes_recorded == 0

    async with session_scope() as session:
        provider = (
            await session.execute(
                select(Provider).where(Provider.provider_id == "acmepay")
            )
        ).scalar_one()
        events = (
            await session.execute(select(func.count()).select_from(ChangeEvent))
        ).scalar_one()

    assert provider.last_checked_at is not None
    assert provider.last_check_error is None
    assert events == 0


async def test_detection_does_not_force_a_project_out_of_an_unrelated_state() -> None:
    """The state machine governs, not the monitor.

    A project still being connected is not one whose migration workflow should
    start because a provider shipped a release. The changes are recorded either
    way — they are facts — but the run does not advance, and `ALLOWED_TRANSITIONS`
    is what decides that rather than anything in this worker.
    """
    project = await _project(state=RunState.PROJECT_CREATED)

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v1")

    async with session_scope() as session:
        (result,) = await monitor_project(
            session, project, adapter_registry=_registry(FixtureProviderAdapter())
        )

    assert result.changes_recorded == 14

    async with session_scope() as session:
        refreshed = await session.get(Project, project.id)
        assert refreshed is not None
        assert refreshed.state is RunState.PROJECT_CREATED


async def test_a_project_with_no_baseline_is_not_treated_as_a_change() -> None:
    """First sight of a provider is not a change from anything.

    Without this, onboarding a repository would immediately fire a full change
    set against a version the project may already be on.
    """
    project = await _project()

    async with session_scope() as session:
        (result,) = await monitor_project(
            session, project, adapter_registry=_registry(FixtureProviderAdapter())
        )

    assert result.baseline is None
    assert result.current == "v2"
    assert result.skipped_reason == "no baseline recorded for this project yet"
    assert result.changes_recorded == 0

    async with session_scope() as session:
        refreshed = await session.get(Project, project.id)
        assert refreshed is not None
        assert refreshed.state is not RunState.CHANGE_DETECTED


async def test_a_new_version_moves_the_project_to_change_detected() -> None:
    """C5-05 acceptance: detection is what starts the workflow."""
    project = await _project()

    async with session_scope() as session:
        await advance_baseline(session, project, "acmepay", "v1")

    async with session_scope() as session:
        await monitor_project(
            session, project, adapter_registry=_registry(FixtureProviderAdapter())
        )

    async with session_scope() as session:
        refreshed = await session.get(Project, project.id)
        assert refreshed is not None
        assert refreshed.state is RunState.CHANGE_DETECTED


async def test_monitoring_scope_follows_the_projects_own_integrations() -> None:
    """A registered adapter for a provider the project does not use is not polled.

    Monitoring follows what the repository actually integrates with, rather than
    a configured list someone has to remember to prune.
    """
    project = await _project(providers=("acmepay",))

    async with session_scope() as session:
        results = await monitor_project(
            session,
            project,
            adapter_registry=_registry(
                FixtureProviderAdapter(), MinimalProviderAdapter()
            ),
        )

    assert [r.provider_id for r in results] == ["acmepay"]


# --- C5-05: no inbound request -------------------------------------------


def test_the_monitor_cannot_reach_the_api_layer() -> None:
    """C5-05 acceptance: monitoring is autonomous, not request-triggered.

    Asserted at the import graph rather than by observing one run, because that
    holds for code nobody has written yet. A monitor that imported a route or a
    request object would be one refactor away from needing a user to poke it —
    and Continuity's whole claim is that it notices changes on its own.
    """
    source = (REPO_ROOT / "backend" / "workers" / "provider_monitor.py").read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    offending = [m for m in imported if m.startswith(("backend.api", "fastapi", "httpx"))]
    assert not offending, f"the monitor depends on the API layer: {offending}"


def test_this_module_never_calls_the_api() -> None:
    """The other half of the same acceptance criterion, about this test file.

    A future edit that reached for the `client` fixture to set something up
    would quietly turn the autonomy proof into a request-driven test that still
    passed. This makes that edit fail here instead.
    """
    source = Path(__file__).read_text()
    tree = ast.parse(source)

    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {m for m in imported if m.startswith(("httpx", "fastapi", "backend.api"))}

    arguments = {
        argument.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        for argument in node.args.args
    }
    assert "client" not in arguments, "this test reached for the HTTP client fixture"
    assert "app" not in arguments
