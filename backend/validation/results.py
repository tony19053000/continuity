"""C7-03: turn real process output into test results. Deterministically.

`02_ARCHITECTURE.md` §13: nothing in `test_results` may be produced by a model.
This module is the only thing that fills that table, and it takes a
`CommandResult` — the output of a process — as its sole source of truth. There
is no parameter here a model could reach.

Parsing prefers machine-readable output (vitest and jest both emit JSON) and
falls back to a runner's summary line. Output it cannot parse raises
`UnparseableTestOutput` rather than returning zeroes: "0 failed" reads as a
pass, and a suite whose output we could not understand has not passed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Final

from backend.shared.errors import ContinuityError
from backend.shared.execution import CommandResult
from backend.shared.redaction import redact

MAX_OUTPUT_EXCERPT: Final = 8_000

#: `= 3 failed, 11 passed, 1 skipped in 0.42s =`
_PYTEST_COUNT: Final = re.compile(
    r"(?P<count>\d+)\s+(?P<outcome>passed|failed|error|errors|skipped|xfailed|xpassed)"
)
_PYTEST_FAILURE: Final = re.compile(r"^(?:FAILED|ERROR)\s+(?P<test_id>\S+)", re.MULTILINE)

#: pytest says so explicitly, and it is not a failure.
_PYTEST_NO_TESTS: Final = re.compile(r"no tests ran", re.IGNORECASE)


class UnparseableTestOutput(ContinuityError):
    """The runner produced output this parser does not understand.

    A typed error rather than a guessed count. Returning zeroes would make an
    unreadable run indistinguishable from a clean one.
    """

    code = "unparseable_test_output"
    status_code = 422
    message = "Test output could not be parsed into a result."


@dataclass(slots=True)
class ParsedTestResult:
    """Counts from one real execution. Never model-produced."""

    suite: str
    command: str
    exit_code: int | None
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failing_test_ids: list[str] = field(default_factory=list)
    duration_ms: int = 0
    timed_out: bool = False
    output_excerpt: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped

    @property
    def ok(self) -> bool:
        """Passed means: it ran, nothing failed, and something was actually run."""
        return not self.timed_out and self.failed == 0 and self.exit_code == 0

    def summary(self) -> dict[str, object]:
        return {
            "suite": self.suite,
            "command": self.command,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "failing_test_ids": list(self.failing_test_ids),
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
        }


def parse_result(result: CommandResult, *, runner: str, suite: str) -> ParsedTestResult:
    """Parse one execution. `result` is the only input; there is no other.

    A timeout is reported as a timeout with no counts. It tells us nothing about
    the code, and folding it into "0 passed, 0 failed" would let a suite that
    hung read as a suite that found nothing wrong.
    """
    text = f"{result.stdout}\n{result.stderr}"
    parsed = ParsedTestResult(
        suite=suite,
        command=" ".join(result.argv),
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        timed_out=result.timed_out,
        # Secret-filtered before storage: repository output is untrusted and
        # this excerpt is read into a browser.
        output_excerpt=redact(text[-MAX_OUTPUT_EXCERPT:]),
    )

    if result.timed_out:
        return parsed

    if runner in {"vitest", "jest"}:
        counts = _parse_json_report(text)
        if counts is None:
            raise UnparseableTestOutput(
                f"{runner} produced no JSON report; exit code {result.exit_code}"
            )
        parsed.passed, parsed.failed, parsed.skipped, parsed.failing_test_ids = counts
        return parsed

    counts = _parse_pytest(text)
    if counts is None:
        raise UnparseableTestOutput(
            f"pytest output carried no recognisable summary; exit code "
            f"{result.exit_code}"
        )
    parsed.passed, parsed.failed, parsed.skipped, parsed.failing_test_ids = counts
    return parsed


def _parse_pytest(text: str) -> tuple[int, int, int, list[str]] | None:
    matches = list(_PYTEST_COUNT.finditer(text))

    if not matches:
        # "no tests ran" is a legitimate, parseable outcome — an empty suite,
        # not unreadable output.
        if _PYTEST_NO_TESTS.search(text):
            return 0, 0, 0, []
        return None

    # Two accumulators per bucket, kept apart on purpose. The summary line's own
    # counts are taken with `max` (a line can appear more than once in a
    # captured log), while errors and xfail/xpass are *additions* to them.
    # Mixing the two in one variable made the result depend on the order the
    # tokens happened to appear in — "1 xfailed, 1 xpassed, 3 passed" lost the
    # xpassed, because `max(1, 3)` overwrote it.
    reported = {"passed": 0, "failed": 0, "skipped": 0}
    extra = {"passed": 0, "failed": 0, "skipped": 0}

    for match in matches:
        outcome = match.group("outcome")
        count = int(match.group("count"))
        if outcome in reported:
            reported[outcome] = max(reported[outcome], count)
        elif outcome in {"error", "errors"}:
            # An error is a failure. A suite that could not import the module it
            # tests has not passed, and a bucket nobody reads is how that looks
            # green.
            extra["failed"] += count
        elif outcome == "xpassed":
            extra["passed"] += count
        elif outcome == "xfailed":
            extra["skipped"] += count

    passed = reported["passed"] + extra["passed"]
    failed = reported["failed"] + extra["failed"]
    skipped = reported["skipped"] + extra["skipped"]

    failing = sorted({m.group("test_id") for m in _PYTEST_FAILURE.finditer(text)})
    return passed, failed, skipped, failing


def _parse_json_report(text: str) -> tuple[int, int, int, list[str]] | None:
    """vitest and jest both emit a JSON object; find and read it.

    The object is located rather than assumed to be the whole of stdout,
    because both runners happily print warnings around it.
    """
    payload = _extract_json_object(text)
    if payload is None:
        return None

    # jest's shape, which vitest also emits.
    passed = _int(payload.get("numPassedTests"))
    failed = _int(payload.get("numFailedTests"))
    skipped = _int(payload.get("numPendingTests")) + _int(payload.get("numTodoTests"))

    failing: list[str] = []
    for suite in payload.get("testResults") or []:
        if not isinstance(suite, dict):
            continue
        suite_name = str(suite.get("name") or suite.get("testFilePath") or "")
        for case in suite.get("assertionResults") or []:
            if isinstance(case, dict) and case.get("status") == "failed":
                title = str(case.get("fullName") or case.get("title") or "")
                failing.append(f"{suite_name}::{title}" if suite_name else title)

    if not payload.keys() & {
        "numPassedTests",
        "numFailedTests",
        "numTotalTests",
        "testResults",
    }:
        return None

    return passed, failed, skipped, sorted(set(failing))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """The first balanced top-level JSON object in the text."""
    for start in (index for index, char in enumerate(text) if char == "{"):
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        payload = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(payload, dict):
                        return payload
                    break
    return None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
