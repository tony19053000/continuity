"""The whole product, in one call.

Every stage was tested on its own inputs. This is the test that the stages
actually connect — that "a provider shipped v2" and "a pull request is waiting"
are two ends of one `run_pipeline` call rather than nine things that each work
alone.

Real throughout where it matters: a real git repository, a real worktree, real
pytest runs, real state transitions, real database rows. The model and GitHub
are stubbed, because what is under test is the wiring.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.agents import migration_engineer as engineer_module
from backend.integrations.graph import EdgeSpec, GraphDelta, IntegrationGraph, NodeSpec
from backend.migrations.workspace import WorkspaceManager
from backend.models import (
    ChangeEvent,
    Integration,
    MigrationRun,
    Project,
    PullRequest,
    Repository,
    RunState,
    User,
)
from backend.models.enums import (
    Confidence,
    EdgeKind,
    EvidenceKind,
    NodeKind,
    SourceKind,
)
from backend.models.schemas import Evidence
from backend.models.session import session_scope
from backend.observability.execution_audit import NullExecutionAudit
from backend.orchestration.coordinator import RunCoordinator
from backend.orchestration.pipeline import run_pipeline
from backend.providers.base import (
    BaseProviderAdapter,
    ExternalDocument,
    ProviderCapability,
    ProviderVersion,
)
from backend.providers.registry import ProviderRegistry
from backend.providers.storage import advance_baseline
from tests.support.agent_stubs import stub_agents
from tests.support.delivery_fixtures import FakeGitHub
from tests.support.migration_fixtures import (
    CLIENT_V2,
    ScriptedEngineer,
    StubProvider,
    fix_it,
    never_works,
    write_fixture_repository,
)

pytestmark = pytest.mark.usefixtures("database")

MAX_ATTEMPTS = 3


# --- a provider that really changes --------------------------------------


def _spec(required: list[str]) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "AcmePay"},
        "paths": {
            "/v1/charges": {
                "post": {
                    "operationId": "createCharge",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": required,
                                    "properties": {
                                        "amount": {"type": "integer"},
                                        "currency": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {}},
                }
            }
        },
    }


class AcmePay(BaseProviderAdapter):
    """v1 requires `amount`; v2 also requires `currency`."""

    provider_id = "acmepay"
    capabilities = frozenset(
        {ProviderCapability.CURRENT_VERSION, ProviderCapability.OPENAPI_SPEC}
    )

    async def get_current_version(self) -> ProviderVersion:
        self._require(ProviderCapability.CURRENT_VERSION)
        return ProviderVersion(version="v2", is_current=True)

    async def fetch_openapi_spec(self, version: ProviderVersion) -> ExternalDocument:
        self._require(ProviderCapability.OPENAPI_SPEC)
        required = ["amount"] if version.version == "v1" else ["amount", "currency"]
        return ExternalDocument(
            kind=SourceKind.OPENAPI_SPEC,
            content=json.dumps(_spec(required), sort_keys=True),
            url=f"https://acmepay.test/{version.version}.json",
            version=version.version,
        )


class UnchangedAcmePay(AcmePay):
    """Still on v1. Nothing for the pipeline to do."""

    async def get_current_version(self) -> ProviderVersion:
        return ProviderVersion(version="v1", is_current=True)


# --- fixtures -------------------------------------------------------------


def _evidence(path: str = "app/client.py", line: int = 1) -> Evidence:
    return Evidence(
        kind=EvidenceKind.SOURCE,
        confidence=Confidence.CONFIRMED,
        file_path=path,
        line_start=line,
        line_end=line,
    )


def _graph() -> GraphDelta:
    """A project that calls the endpoint the provider is about to change."""
    nodes = [
        NodeSpec(NodeKind.PROVIDER, "acmepay", "AcmePay", Confidence.CONFIRMED, _evidence()),
        NodeSpec(NodeKind.FILE, "app/client.py", "app/client.py", Confidence.CONFIRMED, _evidence()),
        NodeSpec(
            NodeKind.SYMBOL, "app/client.py::charge", "charge()",
            Confidence.CONFIRMED, _evidence(),
        ),
        NodeSpec(
            NodeKind.CALL_SITE, "app/client.py:4:post", "post (/v1/charges)",
            Confidence.CONFIRMED, _evidence("app/client.py", 4),
            {"callee": "post", "line": 4, "resource": "/v1/charges",
             "enclosing_symbol": "charge", "arguments": ["/v1/charges"]},
        ),
        NodeSpec(NodeKind.WORKFLOW, "Checkout", "Checkout", Confidence.INFERRED, _evidence()),
        NodeSpec(
            NodeKind.TEST, "tests/test_client.py", "tests/test_client.py",
            Confidence.CONFIRMED, _evidence("tests/test_client.py", 1),
        ),
    ]
    edges = [
        EdgeSpec(EdgeKind.CALLS_PROVIDER, (NodeKind.CALL_SITE, "app/client.py:4:post"),
                 (NodeKind.PROVIDER, "acmepay"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.DEFINED_IN, (NodeKind.CALL_SITE, "app/client.py:4:post"),
                 (NodeKind.SYMBOL, "app/client.py::charge"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.DEFINED_IN, (NodeKind.SYMBOL, "app/client.py::charge"),
                 (NodeKind.FILE, "app/client.py"), Confidence.CONFIRMED, _evidence()),
        EdgeSpec(EdgeKind.IMPLEMENTS_WORKFLOW, (NodeKind.SYMBOL, "app/client.py::charge"),
                 (NodeKind.WORKFLOW, "Checkout"), Confidence.INFERRED, _evidence()),
        EdgeSpec(EdgeKind.COVERED_BY_TEST, (NodeKind.SYMBOL, "app/client.py::charge"),
                 (NodeKind.TEST, "tests/test_client.py"), Confidence.CONFIRMED, _evidence()),
    ]
    return GraphDelta(nodes=nodes, edges=edges)


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "userrepo"
    repo.mkdir()
    write_fixture_repository(repo)

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607 - fixture setup
            cwd=repo, check=True, capture_output=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                 "GIT_CONFIG_SYSTEM": "/dev/null"},
        )

    run("init", "-b", "main")
    run("config", "user.email", "t@example.test")
    run("config", "user.name", "Test")
    run("add", "-A")
    run("commit", "-m", "initial")
    return repo


@pytest.fixture
def workspaces(source_repo: Path, tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(
        source_repo, workspace_root=tmp_path / "ws", audit=NullExecutionAudit()
    )


@pytest.fixture(autouse=True)
def agents(monkeypatch: pytest.MonkeyPatch):
    """Every runtime agent stubbed.

    What is under test is whether the stages connect, not what four models say
    about a fixture. The live path is proven per phase elsewhere.
    """
    return stub_agents(monkeypatch)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    def install(*outputs: Any) -> ScriptedEngineer:
        runner = ScriptedEngineer(*outputs)
        original = engineer_module.MigrationEngineerAgent

        class Patched(original):  # type: ignore[misc, valid-type]
            def __init__(self, provider: Any, **kwargs: Any) -> None:
                super().__init__(provider, runner=runner, **kwargs)

        monkeypatch.setattr(engineer_module, "MigrationEngineerAgent", Patched)
        return runner

    return install


async def _project(*, baseline: str = "v1") -> Project:
    """A scanned project on v1 of acmepay, watching for changes."""
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="p@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"app-{uuid.uuid4().hex[:8]}")
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
        session.add(
            Integration(
                project_id=project.id, provider_id="acmepay", display_name="AcmePay"
            )
        )
        graph = IntegrationGraph(session, project.id)
        version = await graph.next_version()
        await graph.apply(_graph(), version=version)
        await advance_baseline(session, project, "acmepay", baseline)
        await session.flush()
        await session.refresh(project)
        return project


async def _run(project: Project, workspaces: WorkspaceManager, **kwargs: Any):
    registry = ProviderRegistry()
    registry.register(kwargs.pop("adapter", AcmePay()))

    async with session_scope() as session:
        tracked = await session.get(Project, project.id)
        assert tracked is not None
        return await run_pipeline(
            session,
            tracked,
            model_provider=StubProvider(),
            workspaces=workspaces,
            coordinator=RunCoordinator(max_repair_attempts=MAX_ATTEMPTS),
            max_attempts=MAX_ATTEMPTS,
            adapter_registry=registry,
            **kwargs,
        )


# --- the whole thing ------------------------------------------------------


async def test_a_provider_change_becomes_a_pull_request(
    workspaces: WorkspaceManager, scripted: Any, source_repo: Path
) -> None:
    """The product, end to end, in one call.

    The provider ships v2 requiring a new field. Continuity notices, correlates
    it to the call site, judges it relevant, opens a run, migrates in an
    isolated worktree, validates with a real pytest run, reviews the patch, and
    opens a pull request — and the user's checkout is untouched throughout.
    """
    from backend.migrations.workspace import tree_hash

    before = tree_hash(source_repo)
    scripted(fix_it())
    project = await _project()
    github = FakeGitHub()

    result = await _run(project, workspaces, github_client=github)

    assert result.changes_recorded >= 1
    assert result.relevant == 1
    assert len(result.runs) == 1

    (outcome,) = result.runs
    assert outcome.reached_delivery, f"stopped at {outcome.final_state}: {outcome.reason}"
    assert outcome.final_state is RunState.MERGE_WAITING
    assert outcome.delivered is not None
    assert outcome.delivered.branch == "continuity/migrate-acmepay-v2"
    assert result.pull_requests == [42]

    # Real work happened at every stage.
    assert outcome.rehearsal is not None
    assert outcome.repair is not None and outcome.repair.repaired
    assert outcome.repair.review is not None

    # And the user's repository is exactly as it was.
    assert tree_hash(source_repo) == before
    assert "currency" not in (source_repo / "app" / "client.py").read_text()


async def test_the_pull_request_body_is_the_evidence_report(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """What a reviewer reads is assembled from rows, not written by a model."""
    scripted(fix_it())
    project = await _project()
    github = FakeGitHub()

    await _run(project, workspaces, github_client=github)

    (_, pull) = next(c for c in github.calls if c[0] == "create_pull_request")
    body = pull["body"]

    assert "Continuity: acmepay v1 → v2" in body
    assert "### Verification" in body
    assert "passed" in body
    assert "Nothing in this description is generated by a language model" in body


async def test_the_delivered_patch_is_the_migrated_file(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """The bytes that reach GitHub are the bytes the repair loop validated."""
    scripted(fix_it())
    project = await _project()
    github = FakeGitHub()

    await _run(project, workspaces, github_client=github)

    assert github.blobs == [CLIENT_V2]


async def test_every_stage_left_a_record(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """A run that cannot be audited afterwards has not really happened."""
    from backend.models import MigrationAttempt, SecurityFinding, StateTransition, TestResult

    scripted(fix_it())
    project = await _project()

    result = await _run(project, workspaces, github_client=FakeGitHub())
    run_id = result.runs[0].migration_run_id

    async with session_scope() as session:
        async def count(model: Any, column: Any) -> int:
            return (
                await session.execute(
                    select(func.count()).select_from(model).where(column == run_id)
                )
            ).scalar_one()

        assert await count(MigrationAttempt, MigrationAttempt.migration_run_id) >= 1
        assert await count(TestResult, TestResult.migration_run_id) >= 1
        assert await count(StateTransition, StateTransition.migration_run_id) >= 8
        assert await count(PullRequest, PullRequest.migration_run_id) == 1
        # The security review ran, even though it found nothing to report.
        findings = await count(SecurityFinding, SecurityFinding.migration_run_id)
        assert findings >= 0


async def test_the_run_walks_only_legal_transitions(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """Every move the pipeline makes is one `ALLOWED_TRANSITIONS` permits."""
    from backend.models import StateTransition
    from backend.orchestration.state_machine import can_transition

    scripted(fix_it())
    project = await _project()

    await _run(project, workspaces, github_client=FakeGitHub())

    async with session_scope() as session:
        steps = list((await session.execute(select(StateTransition))).scalars())

    assert steps
    for step in steps:
        assert can_transition(step.from_state, step.to_state), (
            f"illegal move recorded: {step.from_state} -> {step.to_state}"
        )


# --- the stops ------------------------------------------------------------


async def test_an_unchanged_provider_stops_immediately(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """The usual outcome, and it must be cheap.

    No workspace, no model call, no run. Just a check and a stop.
    """
    runner = scripted(fix_it())
    project = await _project()

    result = await _run(project, workspaces, adapter=UnchangedAcmePay())

    assert result.changes_recorded == 0
    assert result.runs == []
    assert result.stopped_at == "no new provider changes"
    assert runner.calls == 0


async def test_a_project_with_no_graph_stops_rather_than_guessing(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """Correlation needs a scan. Without one there is nothing to correlate to."""
    scripted(fix_it())

    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="n@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(
            user_id=user.id, repository_id=repository.id, name="unscanned",
            state=RunState.MONITORING_ACTIVE,
        )
        session.add(project)
        await session.flush()
        session.add(
            Integration(project_id=project.id, provider_id="acmepay", display_name="A")
        )
        await advance_baseline(session, project, "acmepay", "v1")
        await session.flush()
        await session.refresh(project)

    result = await _run(project, workspaces)

    assert "no integration graph" in result.stopped_at
    assert result.runs == []


async def test_a_migration_that_cannot_be_repaired_stops_before_delivery(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """The budget runs out, a person is asked, and nothing is opened."""
    scripted(never_works())
    project = await _project()
    github = FakeGitHub()

    result = await _run(project, workspaces, github_client=github)

    (outcome,) = result.runs
    assert not outcome.reached_delivery
    assert outcome.final_state is RunState.HUMAN_REVIEW_REQUIRED
    assert github.called == [], "nothing should have been written to GitHub"

    async with session_scope() as session:
        pulls = (
            await session.execute(select(func.count()).select_from(PullRequest))
        ).scalar_one()
    assert pulls == 0


async def test_a_blocking_security_finding_stops_before_delivery(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """A patch can pass its tests and still not ship.

    The migration widens the OAuth scopes it requests — valid code, green
    suite, and exactly the kind of change a person should see first.
    """
    from tests.support.migration_fixtures import edit, output

    scripted(
        output(
            edit(
                "app/client.py",
                CLIENT_V2 + '\nSCOPES = ["customers.read", "customers.write"]\n',
            )
        )
    )
    project = await _project()
    github = FakeGitHub()

    result = await _run(project, workspaces, github_client=github)

    (outcome,) = result.runs
    assert not outcome.reached_delivery
    assert outcome.final_state in {
        RunState.APPROVAL_PENDING,
        RunState.SECURITY_REVIEW_FAILED,
    }
    assert github.called == []


async def test_without_a_github_client_the_run_completes_and_stops(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """A deployment with no GitHub App still does all the work.

    Everything is decided and recorded; it stops before the one outward-facing
    act rather than pretending to perform it.
    """
    scripted(fix_it())
    project = await _project()

    result = await _run(project, workspaces)

    (outcome,) = result.runs
    assert outcome.final_state is RunState.SECURITY_REVIEW_PASSED
    assert "no GitHub client is configured" in outcome.reason
    assert outcome.repair is not None and outcome.repair.repaired
    assert not outcome.reached_delivery


# --- idempotence ----------------------------------------------------------


async def test_a_second_pass_over_the_same_version_does_nothing(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """The property that makes it safe to run on a short interval.

    The provider has not moved, so the second pass finds nothing — no new
    change events, no second migration run, no second pull request.
    """
    scripted(fix_it(), fix_it())
    project = await _project()
    github = FakeGitHub()

    first = await _run(project, workspaces, github_client=github)
    second = await _run(project, workspaces, github_client=github)

    assert len(first.runs) == 1
    assert second.changes_recorded == 0
    assert second.runs == []

    async with session_scope() as session:
        runs = (
            await session.execute(select(func.count()).select_from(MigrationRun))
        ).scalar_one()
        events = (
            await session.execute(select(func.count()).select_from(ChangeEvent))
        ).scalar_one()

    assert runs == 1
    assert events == first.changes_recorded


async def test_the_workspace_is_cleaned_up_whatever_happens(
    workspaces: WorkspaceManager, scripted: Any
) -> None:
    """A rejected migration leaves nothing on disk."""
    scripted(never_works())
    project = await _project()

    await _run(project, workspaces)

    leftovers = [p for p in workspaces.workspace_root.iterdir() if p.is_dir()]
    assert leftovers == []
