"""Dependency manifest parsing.

Manifests are the most reliable provider signal in a repository: a declared
dependency is a fact, where an import could be dead code. Parsing them
deterministically is what lets the Integration Mapper start from confirmed
ground.

Everything here is `CONFIRMED`. A manifest that cannot be parsed is recorded as
unparsed rather than guessed at.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field

MANIFEST_FILENAMES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "Pipfile",
        "setup.py",
        "setup.cfg",
        "package.json",
        "go.mod",
        "Gemfile",
        "composer.json",
        "pom.xml",
        "build.gradle",
        "Cargo.toml",
    }
)

LOCKFILE_FILENAMES = frozenset(
    {"poetry.lock", "uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock"}
)

_REQUIREMENT_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<spec>[<>=!~^].*)?\s*$"
)


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    constraint: str | None
    manifest_path: str
    line: int | None = None
    dev_only: bool = False


@dataclass(slots=True)
class ManifestAnalysis:
    dependencies: list[Dependency] = field(default_factory=list)
    ecosystem: str | None = None
    parse_error: str | None = None


def parse_manifest(path: str, content: str) -> ManifestAnalysis:
    """Parse a dependency manifest by filename."""
    name = path.rsplit("/", 1)[-1]

    if name == "package.json":
        return _parse_package_json(path, content)
    if name == "pyproject.toml":
        return _parse_pyproject(path, content)
    if name.startswith("requirements") and name.endswith(".txt"):
        return _parse_requirements(path, content, dev_only="dev" in name)

    # Recognised but not yet parsed. Recording the ecosystem is still useful —
    # it tells the mapper what kind of project this is — and claiming zero
    # dependencies would be worse than claiming none were read.
    ecosystems = {
        "go.mod": "go",
        "Gemfile": "ruby",
        "composer.json": "php",
        "pom.xml": "java",
        "build.gradle": "java",
        "Cargo.toml": "rust",
        "Pipfile": "python",
        "setup.py": "python",
        "setup.cfg": "python",
    }
    if name in ecosystems:
        return ManifestAnalysis(
            ecosystem=ecosystems[name], parse_error="parser not implemented for this manifest"
        )

    return ManifestAnalysis(parse_error=f"unrecognised manifest {name!r}")


def _parse_package_json(path: str, content: str) -> ManifestAnalysis:
    analysis = ManifestAnalysis(ecosystem="npm")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        analysis.parse_error = f"invalid JSON: {exc.msg}"
        return analysis

    for section, dev_only in (("dependencies", False), ("devDependencies", True)):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, constraint in block.items():
            analysis.dependencies.append(
                Dependency(
                    name=str(name),
                    constraint=str(constraint) if constraint else None,
                    manifest_path=path,
                    dev_only=dev_only,
                )
            )
    return analysis


def _parse_pyproject(path: str, content: str) -> ManifestAnalysis:
    analysis = ManifestAnalysis(ecosystem="python")
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        analysis.parse_error = f"invalid TOML: {exc}"
        return analysis

    project = data.get("project", {})
    for raw in project.get("dependencies", []) or []:
        name, constraint = _split_requirement(str(raw))
        if name:
            analysis.dependencies.append(
                Dependency(name=name, constraint=constraint, manifest_path=path)
            )

    optional = project.get("optional-dependencies", {}) or {}
    for group, entries in optional.items():
        for raw in entries or []:
            name, constraint = _split_requirement(str(raw))
            if name:
                analysis.dependencies.append(
                    Dependency(
                        name=name,
                        constraint=constraint,
                        manifest_path=path,
                        dev_only=group in {"dev", "test", "lint", "docs"},
                    )
                )

    # Poetry keeps dependencies elsewhere.
    poetry = data.get("tool", {}).get("poetry", {})
    for section, dev_only in (("dependencies", False), ("dev-dependencies", True)):
        block = poetry.get(section, {}) or {}
        for name, constraint in block.items():
            if name == "python":
                continue
            analysis.dependencies.append(
                Dependency(
                    name=str(name),
                    constraint=str(constraint) if isinstance(constraint, str) else None,
                    manifest_path=path,
                    dev_only=dev_only,
                )
            )

    return analysis


def _parse_requirements(path: str, content: str, *, dev_only: bool) -> ManifestAnalysis:
    analysis = ManifestAnalysis(ecosystem="python")
    for number, raw in enumerate(content.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, constraint = _split_requirement(line)
        if name:
            analysis.dependencies.append(
                Dependency(
                    name=name,
                    constraint=constraint,
                    manifest_path=path,
                    line=number,
                    dev_only=dev_only,
                )
            )
    return analysis


def _split_requirement(raw: str) -> tuple[str | None, str | None]:
    """Split `httpx>=0.28` into its name and constraint."""
    cleaned = raw.split(";", 1)[0].strip()
    cleaned = re.sub(r"\[[^\]]*\]", "", cleaned)  # drop extras
    match = _REQUIREMENT_LINE.match(cleaned)
    if not match:
        return None, None
    constraint = (match.group("spec") or "").strip() or None
    return match.group("name"), constraint
