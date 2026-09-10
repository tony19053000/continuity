"""C3-06 acceptance: analysis never modifies the repository.

Read-only analysis is a security guarantee, not merely current behaviour. If a
future change starts writing during a scan, this fails as a security test rather
than quietly altering someone's code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.repository.indexer import build_index
from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.repository.retrieval import ContextRetriever
from backend.workers.repository_scan import hash_tree


@pytest.fixture
def repo_copy(sample_repo: Path) -> Path:
    """A throwaway copy, so a failure cannot damage the committed fixture."""
    return sample_repo


def test_indexing_leaves_the_tree_byte_identical(repo_copy: Path) -> None:
    before = hash_tree(repo_copy)

    build_index(LocalRepositoryAdapter(repo_copy))

    assert hash_tree(repo_copy) == before


def test_retrieval_leaves_the_tree_byte_identical(repo_copy: Path) -> None:
    source = LocalRepositoryAdapter(repo_copy)
    index = build_index(source)
    before = hash_tree(repo_copy)

    retriever = ContextRetriever(index, source, budget_bytes=100_000)
    retriever.for_call_sites("/v1/")
    retriever.for_files(["app/routes.py"])

    assert hash_tree(repo_copy) == before


def test_no_file_is_created_or_deleted_during_a_scan(repo_copy: Path) -> None:
    before = sorted(p.relative_to(repo_copy).as_posix() for p in repo_copy.rglob("*"))

    build_index(LocalRepositoryAdapter(repo_copy))

    after = sorted(p.relative_to(repo_copy).as_posix() for p in repo_copy.rglob("*"))
    assert after == before


def test_the_hash_actually_detects_a_change(repo_copy: Path) -> None:
    """Guards the guard: a hash that never changes proves nothing."""
    before = hash_tree(repo_copy)

    (repo_copy / "app" / "routes.py").write_text("# modified\n")

    assert hash_tree(repo_copy) != before


def test_the_hash_detects_an_added_file(repo_copy: Path) -> None:
    before = hash_tree(repo_copy)

    (repo_copy / "app" / "new.py").write_text("x = 1\n")

    assert hash_tree(repo_copy) != before


def test_the_repository_source_exposes_no_write_method() -> None:
    """Structural: there is nothing to call, not merely nothing called."""
    import inspect

    forbidden = ("write", "create", "delete", "remove", "mkdir", "save", "unlink")
    methods = [
        name
        for name, _ in inspect.getmembers(LocalRepositoryAdapter, inspect.isfunction)
        if not name.startswith("_")
    ]

    offending = [m for m in methods if any(bad in m.lower() for bad in forbidden)]
    assert not offending, f"write-shaped methods on a read-only source: {offending}"
