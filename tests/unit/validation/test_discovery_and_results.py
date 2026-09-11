"""C7-03: discovering how to run tests, and reading what came back.

Everything here is deterministic. The parsers are fed fixture output captured
from the real runners' formats, and the discovery tests build real project
layouts — a `package.json`, a `pyproject.toml`, a `tests/` directory.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.shared.execution import CommandResult, ExecutionStatus
from backend.validation.discovery import (
    JEST,
    PYTEST,
    VITEST,
    discover_test_command,
)
from backend.validation.discovery import (
    TestCommand as RunnerCommand,
)
from backend.validation.discovery import (
    TestCommandNotFound as RunnerNotFound,
)
from backend.validation.results import (
    UnparseableTestOutput,
    parse_result,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _result(stdout: str = "", *, exit_code: int = 0, timed_out: bool = False, stderr: str = ""):
    return CommandResult(
        argv=["python", "-m", "pytest"],
        cwd=Path(__file__).parent,
        status=ExecutionStatus.TIMED_OUT if timed_out else ExecutionStatus.COMPLETED,
        exit_code=None if timed_out else exit_code,
        duration_ms=42,
        stdout=stdout,
        stderr=stderr,
    )


# --- discovery -----------------------------------------------------------


def test_a_package_json_test_script_outranks_everything(tmp_path: Path) -> None:
    """The project saying how to run its tests beats any inference.

    This layout is deliberately contradictory: a `tests/` directory full of
    Python *and* a vitest script. The script is the project's own statement.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run --coverage"}})
    )

    command = discover_test_command(tmp_path)

    assert command.runner == VITEST
    assert "test script" in command.basis


@pytest.mark.parametrize(
    ("filename", "runner"),
    [
        ("pytest.ini", PYTEST),
        ("vitest.config.ts", VITEST),
        ("jest.config.js", JEST),
    ],
)
def test_a_runner_config_file_identifies_the_runner(
    filename: str, runner: str, tmp_path: Path
) -> None:
    (tmp_path / filename).write_text("")

    assert discover_test_command(tmp_path).runner == runner


def test_pyproject_counts_only_when_it_configures_pytest(tmp_path: Path) -> None:
    """Every Python project has a `pyproject.toml`; not every one uses pytest."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')

    with pytest.raises(RunnerNotFound):
        discover_test_command(tmp_path)

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'
    )

    assert discover_test_command(tmp_path).runner == PYTEST


@pytest.mark.parametrize("runner", [VITEST, JEST])
def test_a_declared_dependency_identifies_the_runner(runner: str, tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {runner: "^1.0.0"}})
    )

    assert discover_test_command(tmp_path).runner == runner


def test_a_python_test_directory_is_the_weakest_evidence(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_payments.py").write_text("def test_charge(): pass\n")

    command = discover_test_command(tmp_path)

    assert command.runner == PYTEST
    assert "contains Python test files" in command.basis


def test_a_test_directory_alone_does_not_identify_a_javascript_runner(
    tmp_path: Path,
) -> None:
    """`__tests__/` says a project has tests, not which of several runners runs them.

    Guessing would produce a command that errors in a way that reads like a
    broken suite.
    """
    (tmp_path / "__tests__").mkdir()
    (tmp_path / "__tests__" / "app.test.js").write_text("test('x', () => {});\n")
    (tmp_path / "package.json").write_text(json.dumps({"name": "app"}))

    with pytest.raises(RunnerNotFound):
        discover_test_command(tmp_path)


def test_a_project_with_no_evidence_raises_rather_than_guessing(tmp_path: Path) -> None:
    """C7-03 acceptance, in its most important form.

    "We could not find your tests" and "your tests failed" must never look the
    same, and a default command would make them identical.
    """
    (tmp_path / "README.md").write_text("# nothing here\n")

    with pytest.raises(RunnerNotFound) as raised:
        discover_test_command(tmp_path)

    assert "no test script, runner config, declared dependency" in str(raised.value)


def test_a_malformed_package_json_does_not_crash_discovery(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{ not valid json")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")

    assert discover_test_command(tmp_path).runner == PYTEST


def test_selectors_are_appended_in_the_runners_own_form() -> None:
    pytest_command = RunnerCommand(PYTEST, ["python", "-m", "pytest", "-q"], "x")
    vitest_command = RunnerCommand(VITEST, ["npx", "vitest", "run"], "x")

    assert pytest_command.with_selectors(["tests/test_a.py"]) == [
        "python", "-m", "pytest", "-q", "tests/test_a.py",
    ]
    assert vitest_command.with_selectors(["src/a.test.ts"]) == [
        "npx", "vitest", "run", "--", "src/a.test.ts",
    ]
    assert pytest_command.with_selectors([]) == ["python", "-m", "pytest", "-q"]


# --- pytest output -------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("= 11 passed in 0.42s =", (11, 0, 0)),
        ("= 3 failed, 11 passed in 0.42s =", (11, 3, 0)),
        ("= 3 failed, 11 passed, 2 skipped in 0.4s =", (11, 3, 2)),
        ("= 2 errors in 0.1s =", (0, 2, 0)),
        ("= 1 failed, 1 error, 4 passed in 0.2s =", (4, 2, 0)),
        ("= no tests ran in 0.01s =", (0, 0, 0)),
        ("= 1 xfailed, 1 xpassed, 3 passed in 0.1s =", (4, 0, 1)),
    ],
    ids=["pass", "fail", "skip", "errors", "mixed", "empty", "xfail"],
)
def test_pytest_counts(output: str, expected: tuple[int, int, int]) -> None:
    parsed = parse_result(_result(output, exit_code=1), runner="pytest", suite="s")

    assert (parsed.passed, parsed.failed, parsed.skipped) == expected


def test_pytest_failing_ids_are_extracted() -> None:
    parsed = parse_result(
        _result(
            "FAILED tests/test_pay.py::test_charge - AssertionError\n"
            "ERROR tests/test_pay.py::test_setup\n"
            "= 1 failed, 1 error, 3 passed in 0.2s =\n",
            exit_code=1,
        ),
        runner="pytest",
        suite="s",
    )

    assert parsed.failing_test_ids == [
        "tests/test_pay.py::test_charge",
        "tests/test_pay.py::test_setup",
    ]


# --- vitest and jest JSON ------------------------------------------------


VITEST_JSON = json.dumps(
    {
        "numTotalTests": 5,
        "numPassedTests": 3,
        "numFailedTests": 2,
        "numPendingTests": 0,
        "testResults": [
            {
                "name": "/app/src/pay.test.ts",
                "assertionResults": [
                    {"fullName": "charge sends currency", "status": "failed"},
                    {"fullName": "charge retries", "status": "failed"},
                    {"fullName": "charge works", "status": "passed"},
                ],
            }
        ],
    }
)

JEST_JSON = json.dumps(
    {
        "numTotalTests": 4,
        "numPassedTests": 4,
        "numFailedTests": 0,
        "numPendingTests": 1,
        "numTodoTests": 1,
        "testResults": [{"name": "/app/a.test.js", "assertionResults": []}],
    }
)


def test_vitest_json_is_parsed() -> None:
    parsed = parse_result(_result(VITEST_JSON, exit_code=1), runner="vitest", suite="web")

    assert (parsed.passed, parsed.failed, parsed.skipped) == (3, 2, 0)
    assert parsed.failing_test_ids == [
        "/app/src/pay.test.ts::charge retries",
        "/app/src/pay.test.ts::charge sends currency",
    ]


def test_jest_json_is_parsed() -> None:
    parsed = parse_result(_result(JEST_JSON), runner="jest", suite="web")

    assert (parsed.passed, parsed.failed, parsed.skipped) == (4, 0, 2)
    assert parsed.ok


def test_json_surrounded_by_noise_is_still_found() -> None:
    """Both runners print warnings around their report.

    Assuming the report is the whole of stdout would make a deprecation notice
    look like unparseable output.
    """
    noisy = f"(node:1) Warning: something\n{VITEST_JSON}\nDone in 4.2s\n"

    parsed = parse_result(_result(noisy, exit_code=1), runner="vitest", suite="web")

    assert (parsed.passed, parsed.failed) == (3, 2)


# --- failing to parse ----------------------------------------------------


@pytest.mark.parametrize(
    ("runner", "output"),
    [
        ("pytest", "Traceback (most recent call last):\n  ImportError: no module\n"),
        ("pytest", ""),
        ("vitest", "Cannot find module 'vitest'\n"),
        ("jest", "{ this is not json"),
    ],
    ids=["pytest-traceback", "pytest-empty", "vitest-missing", "jest-broken-json"],
)
def test_unparseable_output_raises_rather_than_guessing(
    runner: str, output: str
) -> None:
    """C7-03 acceptance.

    Returning zeroes here would make a suite that never ran indistinguishable
    from a clean one — "0 failed" reads as a pass.
    """
    with pytest.raises(UnparseableTestOutput):
        parse_result(_result(output, exit_code=1), runner=runner, suite="s")


def test_a_timeout_carries_no_counts_and_is_not_a_pass() -> None:
    parsed = parse_result(
        _result("= 11 passed in 0.4s =", timed_out=True), runner="pytest", suite="s"
    )

    assert parsed.timed_out
    assert not parsed.ok
    assert parsed.exit_code is None
    assert (parsed.passed, parsed.failed) == (0, 0)


def test_a_nonzero_exit_with_no_failures_is_not_a_pass() -> None:
    """A collection error exits non-zero while reporting nothing failed."""
    parsed = parse_result(
        _result("= 3 passed in 0.2s =", exit_code=2), runner="pytest", suite="s"
    )

    assert parsed.failed == 0
    assert not parsed.ok


def test_output_is_secret_filtered_before_storage() -> None:
    """Repository output is untrusted, and the excerpt is read into a browser."""
    from tests.support.secret_samples import GITHUB_TOKEN

    parsed = parse_result(
        _result(f"= 1 passed in 0.1s =\nleaked {GITHUB_TOKEN}\n"),
        runner="pytest",
        suite="s",
    )

    assert GITHUB_TOKEN not in parsed.output_excerpt


# --- the structural guarantee -------------------------------------------


def test_no_code_path_can_build_a_test_result_from_a_model_response() -> None:
    """C7-03 acceptance, asserted structurally.

    `02_ARCHITECTURE.md` §13: nothing in `test_results` may come from a model.
    `store_result` takes a `ParsedTestResult`, and the only way to obtain one is
    `parse_result`, whose sole input is a `CommandResult` — the output of a
    process. This asserts that the modules involved cannot even see a model.
    """
    for relative in ("validation/results.py", "validation/runner.py"):
        source = (REPO_ROOT / "backend" / relative).read_text()
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
        assert not offending, f"{relative} can reach a model: {offending}"


def test_the_only_writer_of_test_results_is_the_runner() -> None:
    """One module writes the table, and it is the one that parses processes."""
    import re

    # Word-boundary, and not the class definition itself: `models/tables.py`
    # declares it, and `ParsedTestResult(` contains the name as a substring.
    construction = re.compile(r"(?<![A-Za-z_])TestResult\(")

    writers = []
    for path in (REPO_ROOT / "backend").rglob("*.py"):
        source = path.read_text()
        relative = path.relative_to(REPO_ROOT / "backend").as_posix()
        if relative == "models/tables.py":
            continue
        if construction.search(source):
            writers.append(relative)

    assert sorted(writers) == ["validation/rehearsal.py", "validation/runner.py"]
