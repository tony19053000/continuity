"""C3-03 acceptance: no path escapes the repository root.

Every one of these is an attack shape. They are in `tests/security/` rather than
`tests/unit/` because a regression here is a directory-traversal vulnerability,
not a bug.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.repository.source import (
    ExcludedPathRequested,
    RepositoryBoundaryViolation,
    normalize_relative_path,
    readable,
    resolve_within,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("VALUE = 1\n")
    (tmp_path / ".env").write_text("SECRET=value\n")
    return tmp_path


ESCAPES = [
    "../etc/passwd",
    "../../etc/passwd",
    "app/../../etc/passwd",
    "app/../../../root/.ssh/id_rsa",
    "/etc/passwd",
    "/",
    "//etc/passwd",
    "~/.ssh/id_rsa",
    "~root/.bashrc",
    "C:/Windows/System32/config",
    "..",
    "app/..%2f..%2fetc",  # only literal traversal is a traversal
]


@pytest.mark.parametrize("path", ESCAPES[:-1])
def test_traversal_and_absolute_paths_are_rejected(path: str) -> None:
    with pytest.raises(RepositoryBoundaryViolation):
        normalize_relative_path(path)


def test_percent_encoded_traversal_is_treated_as_a_literal_name() -> None:
    """`..%2f..` is a filename, not a traversal.

    Decoding it here would *create* the vulnerability it looks like: the
    filesystem never decodes percent-escapes, so a file genuinely named that
    would become unreachable while nothing is made safer.
    """
    assert normalize_relative_path("app/..%2f..%2fetc") == "app/..%2f..%2fetc"


def test_null_bytes_are_rejected() -> None:
    with pytest.raises(RepositoryBoundaryViolation):
        normalize_relative_path("app/main.py\x00.txt")


@pytest.mark.parametrize("path", ESCAPES[:-1])
def test_the_adapter_refuses_every_escape(repo: Path, path: str) -> None:
    adapter = LocalRepositoryAdapter(repo)

    with pytest.raises(RepositoryBoundaryViolation):
        adapter.read_file(path)


def test_a_symlink_pointing_outside_the_root_is_not_followed(
    repo: Path, tmp_path: Path
) -> None:
    """The case a name-only check misses.

    `escape.py` looks like an ordinary file; only resolution reveals its target
    is outside the repository.
    """
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("SENSITIVE\n")
    link = repo / "app" / "escape.py"

    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks are not supported here")

    adapter = LocalRepositoryAdapter(repo)

    listed = [f.path for f in adapter.list_files()]
    assert "app/escape.py" not in listed

    with pytest.raises(RepositoryBoundaryViolation):
        resolve_within(repo, "app/escape.py")


def test_a_symlink_staying_inside_the_root_is_allowed(repo: Path) -> None:
    """Not everything that resolves is an attack."""
    link = repo / "app" / "alias.py"
    try:
        os.symlink(repo / "app" / "main.py", link)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("symlinks are not supported here")

    adapter = LocalRepositoryAdapter(repo)

    assert "app/alias.py" in [f.path for f in adapter.list_files()]


def test_an_excluded_path_cannot_be_read_even_when_asked_for_directly(repo: Path) -> None:
    """Exclusion is enforced at read time, not only during the walk."""
    adapter = LocalRepositoryAdapter(repo)

    with pytest.raises(ExcludedPathRequested):
        adapter.read_file(".env")


def test_listing_never_yields_an_excluded_path(repo: Path) -> None:
    adapter = LocalRepositoryAdapter(repo)

    listed = [f.path for f in adapter.list_files()]

    assert ".env" not in listed
    assert "app/main.py" in listed


def test_the_adapter_records_what_it_pruned(repo: Path) -> None:
    """A silently-pruning source would make the scan summary understate itself."""
    adapter = LocalRepositoryAdapter(repo)
    list(adapter.list_files())

    assert any(path == ".env" for path, _ in adapter.skipped_paths())


def test_readable_is_a_non_raising_form_of_the_same_rules() -> None:
    assert readable("app/main.py")
    assert not readable("../escape")
    assert not readable(".env")


def test_the_adapter_refuses_a_root_that_is_not_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")

    with pytest.raises(RepositoryBoundaryViolation):
        LocalRepositoryAdapter(target)


def test_the_local_adapter_is_named_so_it_cannot_pass_for_github() -> None:
    """It is a development adapter and must never read as production access."""
    adapter_source = Path("backend/repository/local_adapter.py").read_text()

    assert "DEVELOPMENT AND TESTS ONLY" in adapter_source
    assert LocalRepositoryAdapter.__name__.startswith("Local")
