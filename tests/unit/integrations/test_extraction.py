"""C4-02 acceptance: extraction is deterministic, confirmed, and model-free."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.integrations.extraction import extract, provider_candidates
from backend.models.enums import Confidence, EdgeKind, NodeKind
from backend.repository.indexer import build_index
from backend.repository.local_adapter import LocalRepositoryAdapter

SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "expected_extraction.json"


@pytest.fixture
def delta(sample_repo: Path):
    return extract(build_index(LocalRepositoryAdapter(sample_repo)))


def _snapshot(delta) -> dict:
    return {
        "nodes": sorted(
            (
                {
                    "kind": node.kind.value,
                    "key": node.key,
                    "label": node.label,
                    "confidence": node.confidence.value,
                    "attributes": json.loads(json.dumps(node.attributes, default=str)),
                    "has_evidence": node.evidence is not None,
                }
                for node in delta.nodes
            ),
            key=lambda item: (item["kind"], item["key"]),
        ),
        "edges": sorted(
            (
                {
                    "kind": edge.kind.value,
                    "source": f"{edge.source[0].value}:{edge.source[1]}",
                    "target": f"{edge.target[0].value}:{edge.target[1]}",
                    "confidence": edge.confidence.value,
                    "attributes": json.loads(json.dumps(edge.attributes, default=str)),
                }
                for edge in delta.edges
            ),
            key=lambda item: (item["kind"], item["source"], item["target"]),
        ),
    }


def test_extraction_matches_the_committed_snapshot(delta) -> None:
    """Pinned, because everything downstream reads these node keys.

    Regenerate deliberately with:
        uv run python -m scripts.regenerate_extraction_snapshot
    """
    actual = _snapshot(delta)

    if not SNAPSHOT.exists():  # pragma: no cover - first run only
        SNAPSHOT.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.fail("snapshot created; re-run to compare")

    assert actual == json.loads(SNAPSHOT.read_text())


def test_extraction_is_deterministic(sample_repo: Path, delta) -> None:
    again = extract(build_index(LocalRepositoryAdapter(sample_repo)))

    assert _snapshot(delta) == _snapshot(again)


def test_every_extracted_node_is_confirmed(delta) -> None:
    """Extraction produces facts. Judgment is the agent's job, marked INFERRED."""
    assert all(node.confidence is Confidence.CONFIRMED for node in delta.nodes)
    assert all(edge.confidence is Confidence.CONFIRMED for edge in delta.edges)


def test_every_node_carries_evidence(delta) -> None:
    assert all(node.evidence is not None for node in delta.nodes)


def test_no_model_is_invoked_during_extraction(
    sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim that extraction is deterministic, enforced.

    Any touch of the model provider raises, so a future refactor that quietly
    asks a model to help would fail rather than blur the confirmed/inferred
    line.
    """
    import backend.shared.model_provider as provider_module

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("extraction must not touch the model provider")

    monkeypatch.setattr(provider_module, "build_model_provider", explode)
    monkeypatch.setattr(provider_module.GeminiModelProvider, "build_model", explode)
    monkeypatch.setattr(provider_module.BedrockModelProvider, "build_model", explode)

    extract(build_index(LocalRepositoryAdapter(sample_repo)))


# --- What it found -------------------------------------------------------


def test_the_provider_is_identified_and_infrastructure_is_not(sample_repo: Path) -> None:
    """`fastapi` and `httpx` are how you build and call; they are not providers."""
    candidates = provider_candidates(build_index(LocalRepositoryAdapter(sample_repo)))

    assert [c.provider_id for c in candidates] == ["acmepay"]


def test_the_api_version_is_read_from_real_call_sites(sample_repo: Path) -> None:
    """Evidence-backed, not a default assumed on the project's behalf."""
    candidates = provider_candidates(build_index(LocalRepositoryAdapter(sample_repo)))

    assert candidates[0].detected_api_version == "v1"


def test_call_sites_record_the_resource_they_target(delta) -> None:
    resources = {
        node.attributes.get("resource")
        for node in delta.nodes
        if node.kind is NodeKind.CALL_SITE
    }

    assert "/v1/charges" in resources
    assert "/v1/subscriptions/renew" in resources


def test_the_webhook_handler_is_linked_with_its_event_name(delta) -> None:
    """A renamed webhook event is the PRD's canonical breaking change.

    Finding it requires reading a string literal from a comparison, not a call
    argument — `event["type"] == "payment.paid"`.
    """
    webhook_edges = [e for e in delta.edges if e.kind is EdgeKind.HANDLES_WEBHOOK_EVENT]

    assert len(webhook_edges) == 1
    assert webhook_edges[0].source == (NodeKind.SYMBOL, "app/webhooks.py::handle_acmepay_webhook")
    assert "payment.paid" in webhook_edges[0].attributes["events"]


def test_tests_are_linked_to_the_symbols_they_exercise(delta) -> None:
    coverage = [e for e in delta.edges if e.kind is EdgeKind.COVERED_BY_TEST]

    assert coverage
    assert any(e.target == (NodeKind.TEST, "tests/test_payments.py") for e in coverage)


def test_symbols_carry_the_line_span_retrieval_will_slice(delta) -> None:
    symbols = {
        node.key: node.attributes
        for node in delta.nodes
        if node.kind is NodeKind.SYMBOL
    }
    create = symbols["app/services/payment_service.py::create_payment"]

    assert create["line_start"] < create["line_end"]


def test_an_empty_repository_extracts_nothing_without_erroring(tmp_path: Path) -> None:
    """No integrations is a legitimate answer."""
    (tmp_path / "README.md").write_text("# nothing here\n")

    delta = extract(build_index(LocalRepositoryAdapter(tmp_path)))

    assert delta.nodes == []
    assert delta.edges == []
