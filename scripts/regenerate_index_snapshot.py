"""Regenerate the expected index snapshot.

Run deliberately after an intended analyzer change:

    uv run python -m scripts.regenerate_index_snapshot

Then read the diff. A snapshot regenerated without reading the diff defeats
the point of having one.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.repository.indexer import build_index, index_to_snapshot
from backend.repository.local_adapter import LocalRepositoryAdapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample_repo"
SNAPSHOT = ROOT / "tests" / "fixtures" / "expected_index.json"


def main() -> None:
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        # A pristine copy, so a stray artifact — a `__pycache__`, an editor swap
        # file — cannot bake itself into the committed snapshot.
        clean = Path(raw) / "sample_repo"
        shutil.copytree(FIXTURE, clean, ignore=shutil.ignore_patterns("__pycache__"))
        snapshot = index_to_snapshot(build_index(LocalRepositoryAdapter(clean)))

    SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
