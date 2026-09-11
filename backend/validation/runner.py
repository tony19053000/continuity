"""Run a repository's tests and persist what really happened.

The only path into the `test_results` table. Execution goes through
`ExecutionProvider`, parsing is deterministic, and storage takes a
`ParsedTestResult` — which can only be built from a `CommandResult`. There is no
signature here that a model's output fits.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.migrations.workspace import MigrationWorkspace
from backend.models import TestResult
from backend.observability.logging import get_logger
from backend.validation.discovery import TestCommand, discover_test_command
from backend.validation.results import ParsedTestResult, parse_result

logger = get_logger(__name__)

DEFAULT_TEST_TIMEOUT_SECONDS = 600


async def run_tests(
    workspace: MigrationWorkspace,
    *,
    selectors: list[str] | None = None,
    command: TestCommand | None = None,
    suite: str = "repository",
    timeout_seconds: int = DEFAULT_TEST_TIMEOUT_SECONDS,
) -> ParsedTestResult:
    """Discover, execute, and parse. Raises if discovery or parsing fails."""
    resolved = command or discover_test_command(workspace.root)
    argv = resolved.with_selectors(selectors or [])

    result = await workspace.run(argv, timeout_seconds=timeout_seconds)
    parsed = parse_result(result, runner=str(resolved.runner), suite=suite)

    logger.info(
        "continuity.tests_executed",
        extra={
            "runner": str(resolved.runner),
            "passed": parsed.passed,
            "failed": parsed.failed,
            "skipped": parsed.skipped,
            "timed_out": parsed.timed_out,
        },
    )
    return parsed


async def store_result(
    session: AsyncSession,
    parsed: ParsedTestResult,
    *,
    migration_run_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
) -> TestResult:
    """Persist a parsed result.

    Typed to `ParsedTestResult` on purpose: the only way to obtain one is to
    parse a `CommandResult`, so no code path can populate this table from a
    model response.
    """
    row = TestResult(
        migration_run_id=migration_run_id,
        attempt_id=attempt_id,
        suite=parsed.suite,
        command=parsed.command,
        passed=parsed.passed,
        failed=parsed.failed,
        skipped=parsed.skipped,
        exit_code=parsed.exit_code,
        duration_ms=parsed.duration_ms,
        failing_test_ids={"ids": list(parsed.failing_test_ids)},
        raw_output_excerpt=parsed.output_excerpt,
    )
    session.add(row)
    await session.flush()
    return row
