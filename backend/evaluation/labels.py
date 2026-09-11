"""What a labelled case says should have happened.

A case is a directory, because half of it is files:

    tests/fixtures/labelled/<case_id>/
        case.json          the labels
        spec_before.json   the provider's OpenAPI spec at `from_version`
        spec_after.json    ... and at `to_version`
        repo/              the project, as it exists before the migration
        migration/         optional: the files a correct migration produces

`case.json` carries only what a person had to decide. Everything else is read
from the files, so a label and its fixture cannot drift apart.

The labels are the ground truth the metrics are scored against, so they are
validated on load rather than trusted: a case that names an affected file which
is not in its own repository is a broken label, and finding that out during a
metric computation would show up as a product defect that is not one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LabelInvalid(Exception):
    """A labelled case cannot be used as ground truth."""


@dataclass(frozen=True, slots=True)
class ExpectedChange:
    """One change the provider really made, as a person judged it."""

    resource: str
    breaking: bool


@dataclass(frozen=True, slots=True)
class LabelledCase:
    """One case: the world before, the world after, and what should follow."""

    case_id: str
    description: str
    provider_id: str
    from_version: str
    to_version: str
    root: Path

    changes: tuple[ExpectedChange, ...] = ()
    relevant: bool = False
    affected_files: tuple[str, ...] = ()
    affected_workflows: tuple[str, ...] = ()
    migration_expected: bool = False
    delivery_expected: bool = False
    approval_expected: bool = False
    security_violations_expected: int = 0
    notes: str = ""

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def migration(self) -> Path | None:
        candidate = self.root / "migration"
        return candidate if candidate.is_dir() else None

    def spec(self, version: str) -> dict[str, Any]:
        name = "spec_before.json" if version == self.from_version else "spec_after.json"
        spec = json.loads((self.root / name).read_text())
        if not isinstance(spec, dict):
            raise LabelInvalid(f"{self.root / name} is not an object")
        return spec

    def migrated_files(self) -> dict[str, str]:
        """The files a correct migration produces, keyed by repository path."""
        source = self.migration
        if source is None:
            return {}
        return {
            str(path.relative_to(source)): path.read_text()
            for path in sorted(source.rglob("*"))
            if path.is_file()
        }

    def summary(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "description": self.description,
            "provider_id": self.provider_id,
            "versions": f"{self.from_version} -> {self.to_version}",
            "relevant": self.relevant,
            "changes": [
                {"resource": change.resource, "breaking": change.breaking}
                for change in self.changes
            ],
            "affected_files": list(self.affected_files),
            "migration_expected": self.migration_expected,
        }


def load_case(root: Path) -> LabelledCase:
    """Read one case directory, refusing a label it cannot vouch for."""
    manifest = root / "case.json"
    if not manifest.is_file():
        raise LabelInvalid(f"{root} has no case.json")

    try:
        raw = json.loads(manifest.read_text())
    except ValueError as exc:
        raise LabelInvalid(f"{manifest} is not valid JSON: {exc}") from exc

    for required in ("provider_id", "from_version", "to_version", "expected"):
        if required not in raw:
            raise LabelInvalid(f"{manifest} has no {required!r}")

    for name in ("spec_before.json", "spec_after.json"):
        if not (root / name).is_file():
            raise LabelInvalid(f"{root} has no {name}")
    if not (root / "repo").is_dir():
        raise LabelInvalid(f"{root} has no repo/ directory")

    expected = raw["expected"]
    if not isinstance(expected, dict):
        raise LabelInvalid(f"{manifest}: expected must be an object")

    case = LabelledCase(
        case_id=str(raw.get("case_id") or root.name),
        description=str(raw.get("description", "")),
        provider_id=str(raw["provider_id"]),
        from_version=str(raw["from_version"]),
        to_version=str(raw["to_version"]),
        root=root,
        changes=tuple(_changes(manifest, expected.get("changes", []))),
        relevant=bool(expected.get("relevant", False)),
        affected_files=tuple(str(item) for item in expected.get("affected_files", [])),
        affected_workflows=tuple(
            str(item) for item in expected.get("affected_workflows", [])
        ),
        migration_expected=bool(expected.get("migration_expected", False)),
        delivery_expected=bool(expected.get("delivery_expected", False)),
        approval_expected=bool(expected.get("approval_expected", False)),
        security_violations_expected=int(
            expected.get("security_violations_expected", 0)
        ),
        notes=str(raw.get("notes", "")),
    )
    _check_coherent(case)
    return case


def _changes(manifest: Path, raw: Any) -> list[ExpectedChange]:
    if not isinstance(raw, list):
        raise LabelInvalid(f"{manifest}: expected.changes must be a list")
    changes: list[ExpectedChange] = []
    for item in raw:
        if not isinstance(item, dict) or "resource" not in item:
            raise LabelInvalid(f"{manifest}: each change needs a resource")
        changes.append(
            ExpectedChange(
                resource=str(item["resource"]), breaking=bool(item.get("breaking"))
            )
        )
    return changes


def _check_coherent(case: LabelledCase) -> None:
    """Refuse labels that contradict themselves or their own fixture.

    A wrong label does not look like a wrong label in a metric report — it looks
    like a product defect. These are cheap and catch the mistakes a person
    actually makes when writing one.
    """
    for path in case.affected_files:
        if not (case.repo / path).is_file():
            raise LabelInvalid(
                f"{case.case_id}: affected file {path!r} is not in the fixture repo"
            )

    if case.affected_files and not case.relevant:
        raise LabelInvalid(
            f"{case.case_id}: names affected files but is labelled irrelevant"
        )
    if case.migration_expected and not case.relevant:
        raise LabelInvalid(
            f"{case.case_id}: expects a migration for an irrelevant change"
        )
    if case.delivery_expected and not case.migration_expected:
        raise LabelInvalid(
            f"{case.case_id}: expects delivery without expecting a migration"
        )
    if case.migration_expected and case.migration is None:
        raise LabelInvalid(
            f"{case.case_id}: expects a migration but has no migration/ directory"
        )


@dataclass(slots=True)
class CaseSet:
    """Every case in a directory, in a stable order."""

    root: Path
    cases: list[LabelledCase] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Any:
        return iter(self.cases)


def load_cases(root: Path) -> CaseSet:
    """Load every case directory under `root`."""
    if not root.is_dir():
        raise LabelInvalid(f"{root} is not a directory")

    cases = [
        load_case(child)
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / "case.json").is_file()
    ]
    if not cases:
        raise LabelInvalid(f"{root} contains no labelled cases")
    return CaseSet(root=root, cases=cases)
