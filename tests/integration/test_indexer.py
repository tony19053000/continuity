"""C3-04 acceptance: the index is deterministic, complete, and reads nothing it should not."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from backend.repository.indexer import (
    FileKind,
    Language,
    build_index,
    index_to_snapshot,
)
from backend.repository.local_adapter import LocalRepositoryAdapter

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "expected_index.json"


@pytest.fixture
def index(sample_repo: Path):
    return build_index(LocalRepositoryAdapter(sample_repo))


# --- Snapshot ------------------------------------------------------------


def test_the_index_matches_the_committed_snapshot(index) -> None:
    """The whole index, pinned.

    A snapshot rather than scattered assertions because the index is the
    foundation every later phase reads: a silent change in symbol spans or
    detected clients would surface as a mysterious impact-analysis bug three
    phases later.

    Regenerate deliberately with:
        uv run python -m scripts.regenerate_index_snapshot
    """
    actual = index_to_snapshot(index)

    if not SNAPSHOT.exists():  # pragma: no cover - first run only
        SNAPSHOT.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.fail("snapshot created; re-run to compare")

    expected = json.loads(SNAPSHOT.read_text())
    assert actual == expected


def test_indexing_is_deterministic(index, sample_repo: Path) -> None:
    """Two runs over the same tree must be byte-equal."""
    again = build_index(LocalRepositoryAdapter(sample_repo))

    assert index_to_snapshot(index) == index_to_snapshot(again)


# --- What was found ------------------------------------------------------


def test_source_files_are_classified(index) -> None:
    assert index.files["app/services/payment_service.py"].kind is FileKind.SOURCE
    assert index.files["app/services/payment_service.py"].language is Language.PYTHON
    assert index.files["tests/test_payments.py"].kind is FileKind.TEST
    assert index.files["pyproject.toml"].kind is FileKind.MANIFEST
    assert index.files["app/checkout.ts"].language is Language.TYPESCRIPT


def test_dependencies_are_parsed_from_both_ecosystems(index) -> None:
    names = {dependency.name for dependency in index.dependencies}

    assert {"fastapi", "httpx", "acmepay"} <= names
    assert "axios" in names
    assert index.ecosystems == {"python", "npm"}


def test_dev_dependencies_are_distinguished(index) -> None:
    by_name = {dependency.name: dependency for dependency in index.dependencies}

    assert by_name["pytest"].dev_only is True
    assert by_name["fastapi"].dev_only is False


def test_symbols_carry_usable_line_spans(index, sample_repo: Path) -> None:
    """Retrieval slices by these, so a wrong span means the wrong code."""
    file = index.files["app/services/payment_service.py"]
    symbols = {symbol.qualified_name: symbol for symbol in file.symbols}

    assert "create_payment" in symbols
    span = symbols["create_payment"]
    assert span.line_start < span.line_end

    body = sample_repo.joinpath("app/services/payment_service.py").read_text().splitlines()
    assert "def create_payment" in body[span.line_start - 1]


def test_provider_call_sites_are_found_with_their_arguments(index) -> None:
    matches = index.call_sites_matching("/v1/charges")

    assert matches
    file, call = matches[0]
    assert file.path == "app/services/payment_service.py"
    assert "/v1/charges" in call.arguments_preview
    assert call.enclosing_symbol == "create_payment"


def test_routes_are_detected_with_method_and_path(index) -> None:
    routes = {symbol.route_path: symbol for _, symbol in index.routes()}

    assert routes["/checkout"].http_method == "POST"
    assert routes["/webhooks/acmepay"].http_method == "POST"


def test_the_webhook_handler_is_detected(index) -> None:
    handlers = index.webhook_handlers()

    assert [symbol for _, symbol in handlers] == ["handle_acmepay_webhook"]


def test_the_framework_is_detected_from_imports(index) -> None:
    assert "fastapi" in index.frameworks


def test_http_clients_are_detected(index) -> None:
    python_file = index.files["app/services/payment_service.py"]
    ts_file = index.files["app/checkout.ts"]

    assert "httpx" in python_file.http_clients
    assert "axios" in ts_file.http_clients


def test_typescript_results_are_marked_heuristic(index) -> None:
    """TS analysis is regex-based; nothing downstream may treat it as an AST."""
    assert index.files["app/checkout.ts"].heuristic is True
    assert index.files["app/routes.py"].heuristic is False


# --- What was refused ----------------------------------------------------


def test_excluded_paths_are_recorded_with_secrets_distinguished(index) -> None:
    excluded = {entry.path: entry for entry in index.excluded}

    assert excluded[".env"].is_secret is True
    assert excluded["config/deploy.pem"].is_secret is True
    assert excluded["app/logo.png"].is_secret is False
    assert index.summary()["secret_paths_excluded"] == 2


def test_excluded_files_are_absent_from_the_index(index) -> None:
    for path in (".env", "config/deploy.pem", "app/logo.png"):
        assert path not in index.files


def test_excluded_paths_are_never_opened(
    sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strong form: an open() spy proves no read was even attempted.

    Asserting the content is absent would not be enough — a file can be read and
    then discarded, and by then it has been in memory.
    """
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    real_read_text = Path.read_text

    def spy_read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(Path, "read_text", spy_read_text)

    build_index(LocalRepositoryAdapter(sample_repo))

    for forbidden in (".env", "deploy.pem", "logo.png", "node_modules"):
        assert not any(forbidden in path for path in opened), (
            f"{forbidden} was opened during indexing"
        )


def test_oversized_files_are_skipped_by_stat_without_reading(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("x = 1\n" * 200_000)
    (tmp_path / "small.py").write_text("y = 2\n")

    index = build_index(LocalRepositoryAdapter(tmp_path), max_file_bytes=1000)

    big = index.files["big.py"]
    assert big.read_skipped is not None
    assert big.size_bytes > 1000
    # Recorded, but never parsed.
    assert big.symbols == []
    assert index.files["small.py"].read_skipped is None


def test_an_unparseable_file_does_not_fail_the_scan(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def oops(:\n")
    (tmp_path / "fine.py").write_text("def ok():\n    return 1\n")

    index = build_index(LocalRepositoryAdapter(tmp_path))

    assert index.files["broken.py"].parse_error is not None
    assert index.files["fine.py"].symbols
