"""The `ChangeType` partition, which several modules depend on being real.

`spec_derivable()` and `changelog_derived()` are maintained as two independent
literal sets. That is fine only while something checks they stay a partition: a
member added to the enum and to neither set would be silently un-emittable by
the differ and silently un-acceptable from the Change Scout, and every existing
test would still pass.
"""

from __future__ import annotations

from backend.models.enums import ChangeType


def test_the_two_halves_are_disjoint() -> None:
    """No type may be both the differ's answer and the model's.

    An overlapping member would let an INFERRED claim be recorded beside a
    CONFIRMED fact about the same change, and the two would disagree eventually.
    """
    overlap = ChangeType.spec_derivable() & ChangeType.changelog_derived()

    assert overlap == frozenset(), f"claimed by both halves: {sorted(overlap)}"


def test_the_two_halves_cover_every_member() -> None:
    """A member in neither half is one nothing may ever emit."""
    covered = ChangeType.spec_derivable() | ChangeType.changelog_derived()
    missing = frozenset(ChangeType) - covered

    assert missing == frozenset(), (
        f"{sorted(c.value for c in missing)} belongs to neither half — add it to "
        "spec_derivable() or changelog_derived() in backend/models/enums.py"
    )


def test_the_partition_is_not_vacuous() -> None:
    assert len(ChangeType.spec_derivable()) >= 12
    assert len(ChangeType.changelog_derived()) >= 4
