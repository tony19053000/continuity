"""C8-04: the report that makes the claim auditable.

**Every field below traces to a named database column.** That is the whole
design, and it is listed here so a reader can check it rather than take it on
trust:

| report field                | source column                                   |
| --------------------------- | ----------------------------------------------- |
| `provider_id`               | `migration_runs.provider_id`                    |
| `from_version` / `to_version` | `migration_runs.from_version` / `.to_version` |
| `run_state`                 | `migration_runs.state`                          |
| `source_commit`             | `migration_runs.source_commit`                  |
| `target_branch`             | `migration_runs.target_branch`                  |
| `detected_changes`          | count of `change_events` for the provider       |
| `breaking_changes`          | count of `change_events.breaking is true`       |
| `relevant_changes`          | count of `migration_runs` for the project       |
| `change_summary`            | `change_events.change_type` / `.resource`       |
| `affected_workflows`        | `migration_runs.evidence_report`                |
| `affected_files`            | `migration_runs.evidence_report`                |
| `rehearsal`                 | `migration_runs.rehearsal`                      |
| `attempts`                  | `migration_attempts` rows                       |
| `attempts_used`             | count of `migration_attempts`                   |
| `tests`                     | `test_results` rows                             |
| `tests_passed` / `tests_failed` | `test_results.passed` / `.failed`           |
| `security_findings`         | `security_findings` rows                        |
| `permission_expansions`     | `security_findings.category` in the scope set   |
| `approvals`                 | `approvals` rows                                |
| `pull_request`              | `pull_requests` row                             |

**No field is derived from a model response.** The closest thing is a
`diagnosis_summary` written by the Validator, and it is labelled as the
engineer's account rather than presented as fact. Everything numeric — pass
counts, attempt counts, change counts — comes from rows a process wrote.

The report is secret-filtered before it is rendered or posted, because it
becomes a pull request body, which is public to everyone who can see the
repository.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Approval,
    ChangeEvent,
    MigrationAttempt,
    MigrationRun,
    PullRequest,
    SecurityFinding,
    TestResult,
)
from backend.models.enums import FindingCategory
from backend.shared.redaction import redact, redact_deep

#: Findings that mean the migration asks for more access than it had.
PERMISSION_CATEGORIES: Final = frozenset(
    {
        FindingCategory.PRIVILEGE_EXPANSION,
        FindingCategory.OAUTH_SCOPE_CHANGE,
        FindingCategory.AUTHENTICATION_CHANGE,
    }
)


@dataclass(slots=True)
class EvidenceReport:
    """A migration, as the records describe it."""

    migration_run_id: uuid.UUID
    provider_id: str
    from_version: str
    to_version: str
    run_state: str
    source_commit: str | None = None
    target_branch: str | None = None

    detected_changes: int = 0
    breaking_changes: int = 0
    relevant_changes: int = 0
    change_summary: list[str] = field(default_factory=list)

    affected_workflows: list[str] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)

    rehearsal: dict[str, Any] | None = None

    attempts: list[dict[str, Any]] = field(default_factory=list)
    attempts_used: int = 0

    tests: list[dict[str, Any]] = field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0

    security_findings: list[dict[str, Any]] = field(default_factory=list)
    permission_expansions: list[str] = field(default_factory=list)

    approvals: list[dict[str, Any]] = field(default_factory=list)
    pull_request: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "migration_run_id": str(self.migration_run_id),
            "provider_id": self.provider_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "run_state": self.run_state,
            "source_commit": self.source_commit,
            "target_branch": self.target_branch,
            "detected_changes": self.detected_changes,
            "breaking_changes": self.breaking_changes,
            "relevant_changes": self.relevant_changes,
            "change_summary": list(self.change_summary),
            "affected_workflows": list(self.affected_workflows),
            "affected_files": list(self.affected_files),
            "rehearsal": self.rehearsal,
            "attempts": list(self.attempts),
            "attempts_used": self.attempts_used,
            "tests": list(self.tests),
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "security_findings": list(self.security_findings),
            "permission_expansions": list(self.permission_expansions),
            "approvals": list(self.approvals),
            "pull_request": self.pull_request,
        }
        # Filtered as a whole rather than per field: a secret could be anywhere
        # in here, and this is the last stop before a pull request body.
        return redact_deep(payload)  # type: ignore[return-value]

    def as_markdown(self) -> str:
        """The pull request body.

        Written to be read by a person deciding whether to merge, so it leads
        with what changed and what was verified rather than with metadata.
        """
        lines = [
            f"## Continuity: {self.provider_id} {self.from_version} → {self.to_version}",
            "",
            "### What changed upstream",
        ]
        lines.extend(f"- {item}" for item in self.change_summary[:20] or ["- (none recorded)"])
        lines.extend(
            [
                "",
                f"{self.detected_changes} change(s) detected, {self.breaking_changes} "
                f"breaking, {self.relevant_changes} relevant to this project.",
                "",
                "### What it affects here",
            ]
        )
        lines.append(
            "Workflows: " + (", ".join(self.affected_workflows) or "none recorded")
        )
        lines.append("Files: " + (", ".join(self.affected_files) or "none recorded"))

        lines.extend(["", "### Verification"])
        if self.rehearsal:
            lines.append(
                f"Rehearsal: {self.rehearsal.get('outcome', 'unknown')} — "
                f"{self.rehearsal.get('reason', '')}"
            )
        else:
            lines.append("Rehearsal: not run.")

        lines.append(
            f"Tests: {self.tests_passed} passed, {self.tests_failed} failed "
            f"across {len(self.tests)} run(s)."
        )
        lines.append(f"Attempts: {self.attempts_used}.")

        lines.extend(["", "### Security review"])
        if self.security_findings:
            for finding in self.security_findings[:20]:
                lines.append(
                    f"- **{finding['category']}** ({finding['severity']}): "
                    f"{finding['summary']} — policy: {finding['policy_decision']}"
                )
        else:
            lines.append("No findings.")

        if self.permission_expansions:
            lines.append("")
            lines.append(
                "Permission expansion: " + ", ".join(self.permission_expansions)
            )

        if self.approvals:
            lines.extend(["", "### Approvals"])
            for approval in self.approvals:
                lines.append(
                    f"- {approval['trigger']}: {approval['status']}"
                    + (f" (risk {approval['risk']})" if approval.get("risk") else "")
                )

        lines.extend(
            [
                "",
                "---",
                "",
                "Every figure above is read from Continuity's own records of "
                "what ran. Nothing in this description is generated by a "
                "language model.",
            ]
        )
        return redact("\n".join(lines))


async def build_report(session: AsyncSession, run: MigrationRun) -> EvidenceReport:
    """Assemble the report from rows. Nothing here is computed from a model."""
    report = EvidenceReport(
        migration_run_id=run.id,
        provider_id=run.provider_id,
        from_version=run.from_version,
        to_version=run.to_version,
        run_state=run.state.value,
        source_commit=run.source_commit,
        target_branch=run.target_branch,
        rehearsal=dict(run.rehearsal) if run.rehearsal else None,
    )

    await _changes(session, run, report)
    _impact(run, report)
    await _attempts(session, run, report)
    await _tests(session, run, report)
    await _findings(session, run, report)
    await _approvals(session, run, report)
    await _pull_request(session, run, report)
    return report


async def _changes(
    session: AsyncSession, run: MigrationRun, report: EvidenceReport
) -> None:
    rows = list(
        (
            await session.execute(
                select(ChangeEvent)
                .where(
                    ChangeEvent.provider_id == run.provider_id,
                    ChangeEvent.old_version == run.from_version,
                    ChangeEvent.new_version == run.to_version,
                )
                .order_by(ChangeEvent.resource)
            )
        ).scalars()
    )

    report.detected_changes = len(rows)
    report.breaking_changes = sum(1 for row in rows if row.breaking)
    report.change_summary = [
        f"{row.change_type.value} on {row.resource}"
        + (" (breaking)" if row.breaking else "")
        for row in rows
    ]

    report.relevant_changes = (
        await session.execute(
            select(func.count())
            .select_from(MigrationRun)
            .where(MigrationRun.project_id == run.project_id)
        )
    ).scalar_one()


def _impact(run: MigrationRun, report: EvidenceReport) -> None:
    """Affected workflows and files, from the impact assessment stored on the run."""
    stored = run.evidence_report or {}

    def keys(field_name: str) -> list[str]:
        items = stored.get(field_name)
        if not isinstance(items, list):
            return []
        return [
            str(item.get("label") or item.get("key"))
            for item in items
            if isinstance(item, dict)
        ]

    report.affected_workflows = keys("affected_workflows")
    report.affected_files = keys("affected_files")


async def _attempts(
    session: AsyncSession, run: MigrationRun, report: EvidenceReport
) -> None:
    rows = list(
        (
            await session.execute(
                select(MigrationAttempt)
                .where(MigrationAttempt.migration_run_id == run.id)
                .order_by(MigrationAttempt.attempt_number)
            )
        ).scalars()
    )

    report.attempts_used = len(rows)
    report.attempts = [
        {
            "attempt_number": row.attempt_number,
            "outcome": row.outcome.value if row.outcome else None,
            "files_changed": (row.files_changed or {}).get("paths", []),
            # Labelled as an account rather than presented as fact: this is the
            # one field in the report a model wrote.
            "engineer_diagnosis": row.diagnosis_summary,
            "plan_summary": row.plan_summary,
        }
        for row in rows
    ]


async def _tests(
    session: AsyncSession, run: MigrationRun, report: EvidenceReport
) -> None:
    rows = list(
        (
            await session.execute(
                select(TestResult).where(TestResult.migration_run_id == run.id)
            )
        ).scalars()
    )

    report.tests = [
        {
            "suite": row.suite,
            "command": row.command,
            "passed": row.passed,
            "failed": row.failed,
            "skipped": row.skipped,
            "exit_code": row.exit_code,
        }
        for row in rows
    ]
    report.tests_passed = sum(row.passed for row in rows)
    report.tests_failed = sum(row.failed for row in rows)


async def _findings(
    session: AsyncSession, run: MigrationRun, report: EvidenceReport
) -> None:
    rows = list(
        (
            await session.execute(
                select(SecurityFinding).where(SecurityFinding.migration_run_id == run.id)
            )
        ).scalars()
    )

    report.security_findings = [
        {
            "category": row.category.value,
            "severity": row.severity.value,
            "summary": row.summary,
            "recommendation": row.recommendation.value,
            "policy_decision": row.policy_decision.value,
            # Surfaced, not smoothed over (`03_SECURITY_ACCESS.md` §10).
            "disagreed": row.recommendation is not row.policy_decision,
        }
        for row in rows
    ]
    report.permission_expansions = sorted(
        {row.category.value for row in rows if row.category in PERMISSION_CATEGORIES}
    )


async def _approvals(
    session: AsyncSession, run: MigrationRun, report: EvidenceReport
) -> None:
    rows = list(
        (
            await session.execute(
                select(Approval).where(Approval.migration_run_id == run.id)
            )
        ).scalars()
    )

    report.approvals = [
        {
            "trigger": row.trigger,
            "status": row.status.value,
            "risk": row.risk.value,
            "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }
        for row in rows
    ]


async def _pull_request(
    session: AsyncSession, run: MigrationRun, report: EvidenceReport
) -> None:
    row = (
        await session.execute(
            select(PullRequest).where(PullRequest.migration_run_id == run.id)
        )
    ).scalar_one_or_none()

    if row is not None:
        report.pull_request = {
            "number": row.number,
            "url": row.url,
            "branch": row.branch,
            "state": row.state,
            "merged": row.merged,
            "files_changed": row.files_changed,
        }
