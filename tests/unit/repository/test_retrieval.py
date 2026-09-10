"""C3-05 acceptance: retrieval is bounded, evidenced, and filtered."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.repository.indexer import build_index
from backend.repository.local_adapter import LocalRepositoryAdapter
from backend.repository.retrieval import ContextRetriever
from tests.support.secret_samples import AWS_ACCESS_KEY_ID


@pytest.fixture
def retriever(sample_repo: Path):
    source = LocalRepositoryAdapter(sample_repo)
    return ContextRetriever(build_index(source), source, budget_bytes=100_000)


# --- Bounded -------------------------------------------------------------


@pytest.mark.parametrize("budget", [200, 500, 1500, 5000])
def test_retrieved_context_never_exceeds_the_budget(budget: int, sample_repo: Path) -> None:
    source = LocalRepositoryAdapter(sample_repo)
    retriever = ContextRetriever(build_index(source), source, budget_bytes=budget)

    context = retriever.for_call_sites("/v1/")

    assert context.total_bytes <= budget
    assert sum(piece.size_bytes for piece in context.slices) <= budget


def test_an_impossibly_small_budget_yields_nothing_rather_than_overflowing(
    sample_repo: Path,
) -> None:
    source = LocalRepositoryAdapter(sample_repo)
    retriever = ContextRetriever(build_index(source), source, budget_bytes=10)

    context = retriever.for_call_sites("/v1/")

    assert context.slices == []
    assert context.total_bytes == 0
    assert context.truncated


def test_truncation_is_disclosed_in_the_rendered_context(sample_repo: Path) -> None:
    """A model told it has everything will reason as if it does."""
    source = LocalRepositoryAdapter(sample_repo)
    retriever = ContextRetriever(build_index(source), source, budget_bytes=400)

    context = retriever.for_call_sites("client")

    if context.truncated:
        assert "omitted" in context.render()


def test_whole_files_are_still_budgeted(retriever, sample_repo: Path) -> None:
    """"Whole file" is a scope, not an exemption."""
    source = LocalRepositoryAdapter(sample_repo)
    small = ContextRetriever(build_index(source), source, budget_bytes=100)

    context = small.for_files(["app/services/payment_service.py"])

    assert context.total_bytes <= 100


# --- Evidenced -----------------------------------------------------------


def test_every_slice_carries_evidence_with_a_path_and_line_span(retriever) -> None:
    context = retriever.for_call_sites("/v1/")

    assert context.slices
    for piece in context.slices:
        assert piece.evidence.file_path == piece.path
        assert piece.evidence.line_start is not None
        assert piece.evidence.line_end is not None
        assert piece.evidence.line_start <= piece.evidence.line_end


def test_evidence_is_marked_confirmed(retriever) -> None:
    """Source slices are deterministic facts, not model inferences."""
    from backend.models.enums import Confidence

    context = retriever.for_call_sites("/v1/")

    assert all(piece.evidence.confidence is Confidence.CONFIRMED for piece in context.slices)


def test_each_slice_records_why_it_was_selected(retriever) -> None:
    context = retriever.for_call_sites("/v1/charges")

    assert all(piece.reason for piece in context.slices)
    assert any("charges" in piece.reason for piece in context.slices)


# --- Minimal -------------------------------------------------------------


def test_a_symbol_slice_is_scoped_to_the_symbol_not_the_file(
    retriever, sample_repo: Path
) -> None:
    """Sending a whole module to explain one function wastes the budget."""
    context = retriever.for_symbols(
        [("app/services/payment_service.py", "create_payment")]
    )

    assert len(context.slices) == 1
    piece = context.slices[0]
    whole_file = sample_repo.joinpath("app/services/payment_service.py").read_text()

    assert "def create_payment" in piece.content
    assert len(piece.content) < len(whole_file)
    # The neighbouring function must not be dragged in.
    assert "def refund_payment" not in piece.content


def test_the_tightest_enclosing_symbol_is_chosen(retriever) -> None:
    context = retriever.for_call_sites("/v1/subscriptions/renew")

    assert context.slices
    assert "renew_subscription" in context.slices[0].content


def test_an_unknown_symbol_is_skipped_rather_than_guessed(retriever) -> None:
    context = retriever.for_symbols([("app/routes.py", "does_not_exist")])

    assert context.slices == []


def test_an_unknown_path_is_skipped(retriever) -> None:
    context = retriever.for_files(["app/not_a_file.py"])

    assert context.slices == []


# --- Filtered ------------------------------------------------------------


def test_a_secret_in_a_readable_file_is_redacted_on_the_way_out(tmp_path: Path) -> None:
    """Path exclusion cannot help when the secret is hardcoded in source."""
    (tmp_path / "client.py").write_text(
        f'import httpx\n\ndef call():\n    key = "{AWS_ACCESS_KEY_ID}"\n'
        '    return httpx.post("/v1/charges", headers={"k": key})\n'
    )

    source = LocalRepositoryAdapter(tmp_path)
    retriever = ContextRetriever(build_index(source), source, budget_bytes=100_000)

    context = retriever.for_call_sites("/v1/charges")

    assert context.slices
    assert AWS_ACCESS_KEY_ID not in context.render()
    assert "[REDACTED]" in context.render()


def test_evidence_excerpts_are_also_redacted(tmp_path: Path) -> None:
    """Evidence is stored and rendered in the UI, so it is filtered too."""
    (tmp_path / "client.py").write_text(
        f'import httpx\n\ndef call():\n    token = "{AWS_ACCESS_KEY_ID}"\n'
        '    return httpx.post("/v1/charges")\n'
    )

    source = LocalRepositoryAdapter(tmp_path)
    retriever = ContextRetriever(build_index(source), source, budget_bytes=100_000)

    context = retriever.for_call_sites("/v1/charges")

    for piece in context.slices:
        assert AWS_ACCESS_KEY_ID not in (piece.evidence.excerpt or "")


def test_an_excluded_file_can_never_appear_in_context(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"KEY={AWS_ACCESS_KEY_ID}\n")
    (tmp_path / "app.py").write_text('import httpx\n\ndef go():\n    httpx.get("/v1/x")\n')

    source = LocalRepositoryAdapter(tmp_path)
    retriever = ContextRetriever(build_index(source), source, budget_bytes=100_000)

    for context in (retriever.for_files([".env"]), retriever.for_call_sites("/v1/")):
        assert AWS_ACCESS_KEY_ID not in context.render()
