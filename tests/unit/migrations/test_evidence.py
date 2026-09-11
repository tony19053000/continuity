"""C8-04: the report that makes the claim auditable.

The acceptance is provenance: every field traces to a named database column.
The strongest way to test that is to mutate each source record and watch the
corresponding field move — a report field that does not change when its source
changes is not reading from that source.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from backend.migrations.evidence import EvidenceReport, build_report
from backend.models import (
    Approval,
    ChangeEvent,
    MigrationAttempt,
    MigrationRun,
    Project,
    PullRequest,
    Repository,
    RunState,
    SecurityFinding,
    Severity,
    User,
)
from backend.models import (
    TestResult as StoredTestResult,
)
from backend.models.enums import (
    ApprovalStatus,
    AttemptOutcome,
    ChangeType,
    FindingCategory,
    PolicyDecision,
)
from backend.models.session import session_scope

pytestmark = pytest.mark.usefixtures("database")


async def _completed_run() -> MigrationRun:
    """A finished migration, with a row of every kind the report reads."""
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="e@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"app-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(user_id=user.id, repository_id=repository.id, name="p")
        session.add(project)
        await session.flush()

        suffix = uuid.uuid4().hex[:6]
        for resource, breaking in (
            (f"POST /v1/charges request.currency {suffix}", True),
            (f"GET /v2/disputes {suffix}", False),
        ):
            session.add(
                ChangeEvent(
                    provider_id="acmepay",
                    old_version="v1",
                    new_version="v2",
                    change_type=ChangeType.REQUEST_FIELD_REQUIRED
                    if breaking
                    else ChangeType.ENDPOINT_ADDED,
                    resource=resource,
                    breaking=breaking,
                    source={"kind": "openapi_spec"},
                    evidence={"kind": "provider_spec", "confidence": "confirmed"},
                    detected_at=datetime.now(UTC),
                )
            )
        await session.flush()

        event = (
            await session.execute(select(ChangeEvent).where(ChangeEvent.breaking.is_(True)))
        ).scalars().first()
        assert event is not None

        run = MigrationRun(
            project_id=project.id,
            change_event_id=event.id,
            provider_id="acmepay",
            from_version="v1",
            to_version="v2",
            state=RunState.MERGE_WAITING,
            source_commit="abc123",
            target_branch="continuity/migrate-acmepay-v2",
            rehearsal={"outcome": "rehearsal_confirmed", "reason": "1 more test fails"},
            evidence_report={
                "affected_workflows": [{"key": "Checkout", "label": "Checkout"}],
                "affected_files": [
                    {"key": "app/client.py", "label": "app/client.py"}
                ],
            },
        )
        session.add(run)
        await session.flush()

        session.add_all(
            [
                MigrationAttempt(
                    migration_run_id=run.id,
                    attempt_number=1,
                    outcome=AttemptOutcome.FAILED,
                    plan_summary="send currency",
                    files_changed={"paths": ["app/client.py"]},
                    diagnosis_summary="the constant changed but the call did not",
                ),
                MigrationAttempt(
                    migration_run_id=run.id,
                    attempt_number=2,
                    outcome=AttemptOutcome.PASSED,
                    plan_summary="send currency everywhere",
                    files_changed={"paths": ["app/client.py"]},
                ),
                StoredTestResult(
                    migration_run_id=run.id,
                    suite="repository",
                    command="python -m pytest",
                    passed=11,
                    failed=1,
                    skipped=0,
                    exit_code=1,
                ),
                StoredTestResult(
                    migration_run_id=run.id,
                    suite="repository",
                    command="python -m pytest",
                    passed=12,
                    failed=0,
                    skipped=0,
                    exit_code=0,
                ),
                SecurityFinding(
                    migration_run_id=run.id,
                    category=FindingCategory.OAUTH_SCOPE_CHANGE,
                    severity=Severity.HIGH,
                    summary="The patch requests customers.write.",
                    evidence={"kind": "source", "confidence": "confirmed"},
                    recommendation=PolicyDecision.ALLOW,
                    policy_decision=PolicyDecision.ASK,
                ),
                Approval(
                    project_id=project.id,
                    migration_run_id=run.id,
                    trigger="expand_oauth_scope",
                    risk=Severity.HIGH,
                    status=ApprovalStatus.APPROVED,
                    requested_action={"scope": "customers.write"},
                    actor_user_id=user.id,
                    resolved_at=datetime.now(UTC),
                ),
                PullRequest(
                    migration_run_id=run.id,
                    repository_id=repository.id,
                    number=7,
                    url="https://github.com/acme/app/pull/7",
                    branch="continuity/migrate-acmepay-v2",
                    title="Migrate acmepay v1 to v2",
                    state="open",
                    files_changed=1,
                ),
            ]
        )
        await session.flush()
        await session.refresh(run)
        return run


async def _report(run: MigrationRun) -> EvidenceReport:
    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        return await build_report(session, tracked)


# --- the whole report ----------------------------------------------------


async def test_a_report_is_assembled_from_the_records() -> None:
    run = await _completed_run()

    report = await _report(run)

    assert report.provider_id == "acmepay"
    assert (report.from_version, report.to_version) == ("v1", "v2")
    assert report.run_state == "merge_waiting"
    assert report.source_commit == "abc123"
    assert report.detected_changes == 2
    assert report.breaking_changes == 1
    assert report.affected_workflows == ["Checkout"]
    assert report.affected_files == ["app/client.py"]
    assert report.rehearsal is not None
    assert report.attempts_used == 2
    assert report.tests_passed == 23
    assert report.tests_failed == 1
    assert report.permission_expansions == ["oauth_scope_change"]
    assert report.pull_request is not None
    assert report.pull_request["number"] == 7


# --- provenance ----------------------------------------------------------

#: (description, mutation, report field, expected value after the mutation).
#: C8-04 acceptance: mutating each source record changes the matching field.
PROVENANCE: list[tuple[str, str, Any]] = [
    ("migration_runs.provider_id", "provider_id", "otherpay"),
    ("migration_runs.source_commit", "source_commit", "def456"),
    ("migration_runs.target_branch", "target_branch", "continuity/migrate-x-y"),
]


@pytest.mark.parametrize(("column", "report_field", "new_value"), PROVENANCE)
async def test_a_run_column_change_moves_its_report_field(
    column: str, report_field: str, new_value: str
) -> None:
    run = await _completed_run()
    before = getattr(await _report(run), report_field)

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        setattr(tracked, report_field, new_value)
        await session.flush()

    after = getattr(await _report(run), report_field)

    assert before != after
    assert after == new_value, f"{report_field} does not read from {column}"


async def test_adding_a_change_event_moves_the_change_counts() -> None:
    run = await _completed_run()
    before = await _report(run)

    async with session_scope() as session:
        session.add(
            ChangeEvent(
                provider_id="acmepay",
                old_version="v1",
                new_version="v2",
                change_type=ChangeType.ENDPOINT_REMOVED,
                resource=f"DELETE /v1/gone {uuid.uuid4().hex[:6]}",
                breaking=True,
                source={"kind": "openapi_spec"},
                evidence={"kind": "provider_spec", "confidence": "confirmed"},
                detected_at=datetime.now(UTC),
            )
        )
        await session.flush()

    after = await _report(run)

    assert after.detected_changes == before.detected_changes + 1
    assert after.breaking_changes == before.breaking_changes + 1
    assert len(after.change_summary) == len(before.change_summary) + 1


async def test_adding_an_attempt_moves_the_attempt_count() -> None:
    run = await _completed_run()

    async with session_scope() as session:
        session.add(
            MigrationAttempt(
                migration_run_id=run.id,
                attempt_number=3,
                outcome=AttemptOutcome.PASSED,
            )
        )
        await session.flush()

    report = await _report(run)

    assert report.attempts_used == 3
    assert [a["attempt_number"] for a in report.attempts] == [1, 2, 3]


async def test_changing_a_test_result_moves_the_test_counts() -> None:
    """`test_results` is the table a model may never write to.

    The report's pass counts read from it directly, so a figure in a pull
    request body traces to a process that really ran.
    """
    run = await _completed_run()

    async with session_scope() as session:
        row = (
            await session.execute(
                select(StoredTestResult).where(StoredTestResult.migration_run_id == run.id)
            )
        ).scalars().first()
        assert row is not None
        row.passed = 99
        row.failed = 5
        await session.flush()

    report = await _report(run)

    assert report.tests_passed == 99 + 12
    assert report.tests_failed == 5


async def test_changing_a_finding_moves_the_security_section() -> None:
    run = await _completed_run()

    async with session_scope() as session:
        finding = (
            await session.execute(
                select(SecurityFinding).where(SecurityFinding.migration_run_id == run.id)
            )
        ).scalar_one()
        finding.category = FindingCategory.NEW_DEPENDENCY
        finding.summary = "The patch adds left-pad."
        await session.flush()

    report = await _report(run)

    assert report.security_findings[0]["category"] == "new_dependency"
    assert report.security_findings[0]["summary"] == "The patch adds left-pad."
    # No longer a permission-shaped category, so that list empties.
    assert report.permission_expansions == []


async def test_changing_an_approval_moves_the_approval_section() -> None:
    run = await _completed_run()

    async with session_scope() as session:
        approval = (
            await session.execute(
                select(Approval).where(Approval.migration_run_id == run.id)
            )
        ).scalar_one()
        approval.status = ApprovalStatus.REJECTED
        await session.flush()

    report = await _report(run)

    assert report.approvals[0]["status"] == "rejected"


async def test_changing_the_pull_request_moves_its_section() -> None:
    run = await _completed_run()

    async with session_scope() as session:
        pull = (
            await session.execute(
                select(PullRequest).where(PullRequest.migration_run_id == run.id)
            )
        ).scalar_one()
        pull.merged = True
        pull.state = "closed"
        await session.flush()

    report = await _report(run)

    assert report.pull_request is not None
    assert report.pull_request["merged"] is True
    assert report.pull_request["state"] == "closed"


async def test_changing_the_rehearsal_moves_its_section() -> None:
    run = await _completed_run()

    async with session_scope() as session:
        tracked = await session.get(MigrationRun, run.id)
        assert tracked is not None
        tracked.rehearsal = {"outcome": "rehearsal_unavailable", "reason": "no spec"}
        await session.flush()

    report = await _report(run)

    assert report.rehearsal is not None
    assert report.rehearsal["outcome"] == "rehearsal_unavailable"


def test_every_report_field_is_listed_in_the_provenance_table() -> None:
    """The docstring table is the auditable claim, so it must be complete.

    A field added without a documented source is exactly the untraceable figure
    this ticket exists to prevent.
    """
    import backend.migrations.evidence as evidence_module

    documented = evidence_module.__doc__ or ""
    fields = {
        name
        for name in EvidenceReport.__dataclass_fields__
        if name != "migration_run_id"
    }

    missing = [name for name in sorted(fields) if f"`{name}`" not in documented]
    assert not missing, f"report fields with no documented source column: {missing}"


# --- no model, and no secrets -------------------------------------------


def test_the_report_module_cannot_reach_a_model() -> None:
    """C8-04 acceptance: no field is derived from a model response."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3] / "backend" / "migrations" / "evidence.py"
    ).read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    offending = [
        module
        for module in imported
        if module.startswith(("strands", "backend.agents", "backend.shared.model_provider"))
    ]
    assert not offending, f"the evidence report reaches a model: {offending}"


async def test_the_one_model_written_field_is_labelled_as_such() -> None:
    """A diagnosis is the engineer's account, not a finding.

    It is the only text in the report a model produced, and the key name says
    so rather than presenting it as established fact.
    """
    run = await _completed_run()

    report = await _report(run)

    assert report.attempts[0]["engineer_diagnosis"]
    assert "diagnosis" not in report.as_markdown().lower().split("engineer")[0]


async def test_the_report_is_secret_filtered() -> None:
    """It becomes a pull request body, visible to everyone with repo access."""
    from tests.support.secret_samples import GITHUB_TOKEN

    run = await _completed_run()

    async with session_scope() as session:
        finding = (
            await session.execute(
                select(SecurityFinding).where(SecurityFinding.migration_run_id == run.id)
            )
        ).scalar_one()
        finding.summary = f"The patch added TOKEN={GITHUB_TOKEN}"
        await session.flush()

    report = await _report(run)

    assert GITHUB_TOKEN not in str(report.as_dict())
    assert GITHUB_TOKEN not in report.as_markdown()


async def test_the_markdown_body_states_that_nothing_is_model_generated() -> None:
    """The claim a reviewer is being asked to trust, made explicitly."""
    run = await _completed_run()

    body = (await _report(run)).as_markdown()

    assert "acmepay v1 → v2" in body
    assert "11 passed" not in body  # aggregated, not a raw row dump
    assert "23 passed, 1 failed" in body
    assert "Nothing in this description is generated by a language model" in body
