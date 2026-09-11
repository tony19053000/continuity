"""C9-02 end to end: a real manifest, on a real repository record, really read.

`tests/unit/verification/` proves the manifest parser and the request shape.
This proves the part that only shows up when the pieces are joined: that the
manifest is found where a project would put it, that the pre-merge observation
is stored on the run and read back at verification time, and that each of the
three outcomes is recorded under the name it deserves.

The environment itself is an `httpx.MockTransport`. That is not a mocked-out
Continuity — every line of Continuity's own code runs — it is a stand-in for
someone else's staging deployment, which a test suite has no business standing
up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from backend.models import ChangeEvent, MigrationRun, Project, Repository, User
from backend.models.enums import ChangeType, RunState
from backend.models.session import create_all, session_scope
from backend.shared.config import Environment, Settings
from backend.verification.environment import MANIFEST_PATH
from backend.workers.post_merge_verify import (
    DISABLED,
    FAILED,
    MANIFEST_INVALID,
    NOT_CONFIGURED,
    PASSED,
    PRE_MERGE_OBSERVATION_KEY,
    RELEASE_VERIFICATION_KEY,
    observe_before_merge,
    verify_environment,
)

MANIFEST = {
    "base_url": "https://staging.example.test",
    "timeout_seconds": 5,
    "checks": [
        {"name": "health", "method": "GET", "path": "/healthz", "expect_status": 200},
        {"name": "checkout", "method": "GET", "path": "/checkout", "expect_status": 200},
    ],
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        CONTINUITY_ENV=Environment.TEST,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'release.db'}",
        SESSION_SECRET=SecretStr("release-verification-secret-value"),
        RELEASE_VERIFICATION_ENABLED=True,
        _env_file=None,
    )


@pytest.fixture
def off(settings: Settings) -> Settings:
    return settings.model_copy(update={"RELEASE_VERIFICATION_ENABLED": False})


@pytest_asyncio.fixture(autouse=True)
async def database(settings: Settings) -> AsyncIterator[None]:
    from backend.models.session import dispose_engine, init_engine

    init_engine(settings)
    await create_all()
    try:
        yield
    finally:
        await dispose_engine()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "commerce-api"
    (root / ".continuity").mkdir(parents=True)
    return root


def _write_manifest(root: Path, manifest: object) -> None:
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (root / MANIFEST_PATH).write_text(text)


def _healthy(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="ok")


def _checkout_broken(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/checkout":
        return httpx.Response(500, text="internal error")
    return httpx.Response(200, text="ok")


async def _run_for(root: Path) -> MigrationRun:
    """A merged run on a project whose repository is this checkout."""
    async with session_scope() as session:
        user = User(email="dev@example.test", google_subject=uuid.uuid4().hex)
        session.add(user)
        await session.flush()

        repository = Repository(
            owner="acme", name="commerce-api", default_branch="main",
            local_path=str(root),
        )
        session.add(repository)
        await session.flush()

        project = Project(
            user_id=user.id,
            repository_id=repository.id,
            name="commerce-api",
            state=RunState.MONITORING_ACTIVE,
        )
        session.add(project)
        await session.flush()

        event = ChangeEvent(
            provider_id="acmepay",
            old_version="v1",
            new_version="v2",
            change_type=ChangeType.REQUEST_FIELD_REQUIRED,
            resource="POST /v1/charges request.currency",
            breaking=True,
            source={"kind": "openapi_spec"},
            evidence={"kind": "provider_spec", "confidence": "confirmed"},
            detected_at=datetime.now(UTC),
        )
        session.add(event)
        await session.flush()

        run = MigrationRun(
            project_id=project.id,
            change_event_id=event.id,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            state=RunState.MERGE_WAITING,
        )
        session.add(run)
        await session.flush()
        return run


async def test_a_project_that_declared_checks_has_them_run(
    project_root: Path, settings: Settings
) -> None:
    _write_manifest(project_root, MANIFEST)
    run = await _run_for(project_root)

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        result = await verify_environment(
            session,
            tracked,
            settings=settings,
            transport=httpx.MockTransport(_healthy),
        )

    assert result.verification == PASSED
    assert result.checks_run == 2
    assert result.assessment is not None and result.assessment.passed


async def test_a_regression_against_a_recorded_baseline_fails_the_run(
    project_root: Path, settings: Settings
) -> None:
    """The whole reason the pre-merge observation is taken."""
    _write_manifest(project_root, MANIFEST)
    run = await _run_for(project_root)

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        repository = await _repository(session, tracked)
        before = await observe_before_merge(
            session,
            tracked,
            repository,
            settings=settings,
            transport=httpx.MockTransport(_healthy),
        )
        assert before is not None and not before.failures

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        assert PRE_MERGE_OBSERVATION_KEY in (tracked.evidence_report or {})

        result = await verify_environment(
            session,
            tracked,
            settings=settings,
            transport=httpx.MockTransport(_checkout_broken),
        )

    assert result.verification == FAILED
    assert result.assessment is not None
    assert [item.name for item in result.assessment.regressions] == ["checkout"]
    assert result.assessment.compared_with_baseline


async def test_a_check_that_was_already_broken_is_not_called_a_regression(
    project_root: Path, settings: Settings
) -> None:
    _write_manifest(project_root, MANIFEST)
    run = await _run_for(project_root)
    broken = httpx.MockTransport(_checkout_broken)

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        await observe_before_merge(
            session,
            tracked,
            await _repository(session, tracked),
            settings=settings,
            transport=broken,
        )

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        result = await verify_environment(
            session, tracked, settings=settings, transport=broken
        )

    assert result.verification == PASSED, "broken before and after is not this migration"
    assert result.assessment is not None
    assert result.assessment.already_failing == ["checkout"]
    assert result.assessment.regressions == []


async def test_a_project_with_no_manifest_is_recorded_as_not_configured(
    project_root: Path, settings: Settings
) -> None:
    run = await _run_for(project_root)

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        result = await verify_environment(
            session,
            tracked,
            settings=settings,
            transport=httpx.MockTransport(_healthy),
        )

    assert result.verification == NOT_CONFIGURED
    assert not result.blocks
    assert result.assessment is None

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        stored = (tracked.evidence_report or {})[RELEASE_VERIFICATION_KEY]
    assert stored["verification"] == NOT_CONFIGURED
    assert "assessment" not in stored, "nothing was assessed, so nothing is reported"


async def test_a_broken_manifest_is_reported_and_does_not_hold_the_merge(
    project_root: Path, settings: Settings
) -> None:
    """A typo must not block every future merge, and must not read as a pass."""
    _write_manifest(project_root, "{ this is not json")
    run = await _run_for(project_root)

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        result = await verify_environment(
            session,
            tracked,
            settings=settings,
            transport=httpx.MockTransport(_healthy),
        )

    assert result.verification == MANIFEST_INVALID
    assert not result.blocks
    assert not result.configured
    assert "not valid JSON" in result.detail


async def test_nothing_is_requested_while_the_feature_is_off(
    project_root: Path, off: Settings
) -> None:
    """The opt-in is the control. It has to actually stop the requests."""
    _write_manifest(project_root, MANIFEST)
    run = await _run_for(project_root)
    requested: list[str] = []

    def watcher(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="ok")

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        repository = await _repository(session, tracked)
        before = await observe_before_merge(
            session, tracked, repository, settings=off,
            transport=httpx.MockTransport(watcher),
        )
        result = await verify_environment(
            session, tracked, settings=off, transport=httpx.MockTransport(watcher)
        )

    assert before is None
    assert result.verification == DISABLED
    assert requested == []


async def _repository(session: object, run: MigrationRun) -> Repository:
    project = await session.get(Project, run.project_id)  # type: ignore[attr-defined]
    assert project is not None
    repository = await session.get(Repository, project.repository_id)  # type: ignore[attr-defined]
    assert repository is not None
    return repository


async def test_the_guardian_is_told_which_files_the_migration_changed(
    project_root: Path, settings: Settings
) -> None:
    """A regression is easier to attribute when you know what moved.

    This asserts the *shape* of what the security review stores, not just that
    the reader compiles: `_changed_files` read a key the review had never
    written, so it returned nothing on every real run and the Guardian was
    always told the migration changed no files.
    """
    from backend.agents.security_reviewer import review_patch
    from backend.workers.post_merge_verify import _changed_files

    diff = (
        "--- a/app/payments.py\n"
        "+++ b/app/payments.py\n"
        '+SCOPES = "customers.write"\n'
    )
    review = await review_patch(
        provider_id="acmepay",
        diff=diff,
        changed_dependencies=[],
        modifies_tests=False,
    )
    assert review.findings, "the fixture diff must produce a finding to record"

    run = await _run_for(project_root)
    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        # Stored exactly as the repair loop stores it.
        tracked.evidence_report = {"security_review": review.report()}
        await session.flush()

        assert _changed_files(tracked) == ["app/payments.py"]
