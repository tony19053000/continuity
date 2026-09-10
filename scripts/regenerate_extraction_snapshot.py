"""Regenerate the expected extraction snapshot.

Run deliberately after an intended extraction change, then read the diff:

    uv run python -m scripts.regenerate_extraction_snapshot
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from backend.integrations.extraction import extract
from backend.repository.indexer import build_index
from backend.repository.local_adapter import LocalRepositoryAdapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample_repo"
SNAPSHOT = ROOT / "tests" / "fixtures" / "expected_extraction.json"


def main() -> None:
    from tests.unit.integrations.test_extraction import _snapshot

    with tempfile.TemporaryDirectory() as raw:
        # A pristine copy, so a stray artifact cannot bake itself into the file.
        clean = Path(raw) / "sample_repo"
        shutil.copytree(FIXTURE, clean, ignore=shutil.ignore_patterns("__pycache__"))
        delta = extract(build_index(LocalRepositoryAdapter(clean)))

    SNAPSHOT.write_text(json.dumps(_snapshot(delta), indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
