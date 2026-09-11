"""C7-03: work out how to run this project's tests, from its own files.

Deterministic, and it fails loudly. Guessing `pytest` at a JavaScript project
produces a command that errors in a way that reads like a broken test suite, so
discovery either finds evidence of a runner or reports that it found none.

Evidence, in order of authority:

1. an explicit `test` script in `package.json` — the project saying so itself;
2. a runner's own config file (`pytest.ini`, `vitest.config.ts`, `jest.config.js`);
3. a runner declared as a dependency in a manifest;
4. a conventional test directory.

A project with no evidence at all returns nothing, and the caller reports that
rather than running something hopeful.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.shared.errors import ContinuityError


class TestCommandNotFound(ContinuityError):
    """No test runner could be identified for this repository.

    A typed failure rather than a default command: "we could not find your
    tests" and "your tests failed" must never look the same.
    """

    code = "test_command_not_found"
    status_code = 422
    message = "No test runner could be identified for this repository."


class TestRunner(str):
    """Just a name, kept as a type so signatures read clearly."""


PYTEST: Final = TestRunner("pytest")
VITEST: Final = TestRunner("vitest")
JEST: Final = TestRunner("jest")


@dataclass(frozen=True, slots=True)
class TestCommand:
    """How to run a project's tests, and why we think so."""

    runner: TestRunner
    argv: list[str]
    basis: str

    def with_selectors(self, selectors: list[str]) -> list[str]:
        """The command narrowed to specific tests.

        Selection matters for cost and for signal: running the whole suite to
        learn about three files buries the answer and takes far longer.
        """
        if not selectors:
            return list(self.argv)
        if self.runner is PYTEST or self.runner == PYTEST:
            return [*self.argv, *selectors]
        # vitest and jest both take paths positionally after `--`.
        return [*self.argv, "--", *selectors]

    def summary(self) -> dict[str, object]:
        return {"runner": str(self.runner), "argv": list(self.argv), "basis": self.basis}


#: Config files that identify a runner outright.
_CONFIG_FILES: Final[dict[str, TestRunner]] = {
    "pytest.ini": PYTEST,
    "tox.ini": PYTEST,
    "vitest.config.ts": VITEST,
    "vitest.config.js": VITEST,
    "vitest.config.mts": VITEST,
    "jest.config.js": JEST,
    "jest.config.ts": JEST,
    "jest.config.mjs": JEST,
    "jest.config.json": JEST,
}

_TEST_DIRECTORIES: Final = ("tests", "test", "__tests__")

#: Machine-readable output, so parsing is not screen-scraping. Both runners
#: support JSON reporting, and a report file cannot be confused with a log line.
#: No `-q`. A project whose own `addopts` already carries it would get `-q -q`,
#: and two of them suppress the summary line entirely — leaving a suite that ran
#: perfectly well with no counts to parse. Verbosity is the project's choice;
#: the summary line is not optional for us.
PYTEST_ARGV: Final = ["python", "-m", "pytest", "-p", "no:cacheprovider"]
VITEST_ARGV: Final = ["npx", "vitest", "run", "--reporter=json"]
JEST_ARGV: Final = ["npx", "jest", "--json"]

_ARGV_FOR: Final[dict[TestRunner, list[str]]] = {
    PYTEST: PYTEST_ARGV,
    VITEST: VITEST_ARGV,
    JEST: JEST_ARGV,
}


def discover_test_command(root: Path) -> TestCommand:
    """Find how to run this repository's tests, or raise."""
    for finder in (
        _from_package_json_script,
        _from_config_file,
        _from_manifest_dependency,
        _from_test_directory,
    ):
        found = finder(root)
        if found is not None:
            return found

    raise TestCommandNotFound(
        f"no pytest, vitest, or jest evidence found in {root.name}: no test "
        "script, runner config, declared dependency, or test directory"
    )


def _read(root: Path, name: str) -> str | None:
    path = root / name
    try:
        return path.read_text() if path.is_file() else None
    except OSError:
        return None


def _from_package_json_script(root: Path) -> TestCommand | None:
    """The project's own `test` script, which outranks every inference."""
    content = _read(root, "package.json")
    if content is None:
        return None
    try:
        scripts = (json.loads(content).get("scripts") or {})
    except (json.JSONDecodeError, AttributeError):
        return None

    script = str(scripts.get("test", ""))
    if not script:
        return None

    for runner in (VITEST, JEST):
        if re.search(rf"\b{runner}\b", script):
            return TestCommand(
                runner=runner,
                argv=_ARGV_FOR[runner],
                basis=f"package.json test script runs {runner}",
            )
    if re.search(r"\bpytest\b", script):
        return TestCommand(
            runner=PYTEST, argv=PYTEST_ARGV, basis="package.json test script runs pytest"
        )
    return None


def _from_config_file(root: Path) -> TestCommand | None:
    for name, runner in _CONFIG_FILES.items():
        if (root / name).is_file():
            return TestCommand(
                runner=runner, argv=_ARGV_FOR[runner], basis=f"{name} is present"
            )

    # `pyproject.toml` counts only when it actually configures pytest.
    content = _read(root, "pyproject.toml")
    if content is not None:
        try:
            data = tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            data = {}
        if "pytest" in (data.get("tool") or {}):
            return TestCommand(
                runner=PYTEST,
                argv=PYTEST_ARGV,
                basis="pyproject.toml configures [tool.pytest]",
            )
    return None


def _from_manifest_dependency(root: Path) -> TestCommand | None:
    content = _read(root, "package.json")
    if content is not None:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {}
        declared: set[str] = set()
        for key in ("dependencies", "devDependencies"):
            declared.update((data.get(key) or {}).keys())
        for runner in (VITEST, JEST):
            if runner in declared:
                return TestCommand(
                    runner=runner,
                    argv=_ARGV_FOR[runner],
                    basis=f"{runner} is a declared dependency",
                )

    for name in ("pyproject.toml", "requirements.txt"):
        content = _read(root, name)
        if content and re.search(r"(^|[\s\"'])pytest\b", content, re.MULTILINE):
            return TestCommand(
                runner=PYTEST, argv=PYTEST_ARGV, basis=f"pytest is declared in {name}"
            )
    return None


def _from_test_directory(root: Path) -> TestCommand | None:
    """Weakest evidence, and only for Python.

    A `tests/` directory in a JavaScript project says nothing about which of
    several runners it uses, and picking one would be a guess.
    """
    for name in _TEST_DIRECTORIES:
        directory = root / name
        if directory.is_dir() and any(directory.rglob("test_*.py")):
            return TestCommand(
                runner=PYTEST,
                argv=PYTEST_ARGV,
                basis=f"{name}/ contains Python test files",
            )
    return None
