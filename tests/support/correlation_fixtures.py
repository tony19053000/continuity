"""A fixture project graph and change set for C6-02 / C6-03.

Shaped like a real integration rather than a minimal one, because the property
under test is *selectivity*: most provider changes touch nothing, and a graph
with one call site cannot demonstrate that. This has four call sites across two
endpoints, a webhook handler, two workflows, tests, and a declared scope.
"""

from __future__ import annotations

from backend.integrations.graph import EdgeSpec, GraphDelta, NodeSpec
from backend.models.enums import (
    ChangeType,
    Confidence,
    EdgeKind,
    EvidenceKind,
    NodeKind,
    SourceKind,
)
from backend.models.schemas import Evidence, ProviderChange, SourceRef

PROVIDER = "acmepay"

SPEC_SOURCE = SourceRef(kind=SourceKind.OPENAPI_SPEC, url="https://acmepay.test/spec")


def _evidence(path: str = "app/payments.py", line: int = 10) -> Evidence:
    return Evidence(
        kind=EvidenceKind.SOURCE,
        confidence=Confidence.CONFIRMED,
        file_path=path,
        line_start=line,
        line_end=line,
    )


def fixture_graph() -> GraphDelta:
    """Two endpoints called from three places, plus a webhook handler."""
    nodes = [
        NodeSpec(NodeKind.PROVIDER, PROVIDER, "AcmePay", Confidence.CONFIRMED, _evidence()),
        NodeSpec(NodeKind.SDK, "acmepay==1.4", "acmepay", Confidence.CONFIRMED, _evidence()),
        NodeSpec(NodeKind.FILE, "app/payments.py", "app/payments.py", Confidence.CONFIRMED, _evidence()),
        NodeSpec(NodeKind.FILE, "app/webhooks.py", "app/webhooks.py", Confidence.CONFIRMED, _evidence()),
        NodeSpec(
            NodeKind.SYMBOL, "app/payments.py::charge", "charge()",
            Confidence.CONFIRMED, _evidence(),
        ),
        NodeSpec(
            NodeKind.SYMBOL, "app/payments.py::refund", "refund()",
            Confidence.CONFIRMED, _evidence("app/payments.py", 40),
        ),
        NodeSpec(
            NodeKind.SYMBOL, "app/webhooks.py::on_paid", "on_paid()",
            Confidence.CONFIRMED, _evidence("app/webhooks.py", 8),
        ),
        # Two call sites on /v1/charges, in different functions.
        NodeSpec(
            NodeKind.CALL_SITE, "app/payments.py:12:post", "post (/v1/charges)",
            Confidence.CONFIRMED, _evidence("app/payments.py", 12),
            {"callee": "post", "line": 12, "resource": "/v1/charges",
             "enclosing_symbol": "charge", "arguments": ["/v1/charges", "status=pending"]},
        ),
        NodeSpec(
            NodeKind.CALL_SITE, "app/payments.py:20:post", "post (/v1/charges)",
            Confidence.CONFIRMED, _evidence("app/payments.py", 20),
            {"callee": "post", "line": 20, "resource": "/v1/charges",
             "enclosing_symbol": "charge", "arguments": ["/v1/charges", "status=paid"]},
        ),
        # One on a templated path, to prove segment-wise matching.
        NodeSpec(
            NodeKind.CALL_SITE, "app/payments.py:44:get", "get (/v1/refunds/{id})",
            Confidence.CONFIRMED, _evidence("app/payments.py", 44),
            {"callee": "get", "line": 44, "resource": "/v1/refunds/{id}",
             "enclosing_symbol": "refund", "arguments": ["/v1/refunds/{id}"]},
        ),
        NodeSpec(NodeKind.WORKFLOW, "Checkout", "Checkout", Confidence.INFERRED, _evidence()),
        NodeSpec(NodeKind.WORKFLOW, "Refunds", "Refunds", Confidence.INFERRED, _evidence()),
        NodeSpec(
            NodeKind.WORKFLOW, "Payment Events", "Payment Events",
            Confidence.INFERRED, _evidence(),
        ),
        NodeSpec(
            NodeKind.TEST, "tests/test_payments.py", "tests/test_payments.py",
            Confidence.CONFIRMED, _evidence("tests/test_payments.py", 1),
        ),
        NodeSpec(
            NodeKind.TEST, "tests/test_webhooks.py", "tests/test_webhooks.py",
            Confidence.CONFIRMED, _evidence("tests/test_webhooks.py", 1),
        ),
        NodeSpec(
            NodeKind.PERMISSION, f"{PROVIDER}:customers.read", "customers.read",
            Confidence.CONFIRMED, _evidence(),
            {"scope": "customers.read", "provider": PROVIDER},
        ),
    ]

    def calls(call_key: str, symbol: str) -> list[EdgeSpec]:
        return [
            EdgeSpec(
                EdgeKind.CALLS_PROVIDER, (NodeKind.CALL_SITE, call_key),
                (NodeKind.PROVIDER, PROVIDER), Confidence.CONFIRMED, _evidence(),
            ),
            EdgeSpec(
                EdgeKind.DEFINED_IN, (NodeKind.CALL_SITE, call_key),
                (NodeKind.SYMBOL, symbol), Confidence.CONFIRMED, _evidence(),
            ),
        ]

    edges = [
        EdgeSpec(
            EdgeKind.DECLARES_SDK, (NodeKind.PROVIDER, PROVIDER),
            (NodeKind.SDK, "acmepay==1.4"), Confidence.CONFIRMED, _evidence(),
        ),
        *calls("app/payments.py:12:post", "app/payments.py::charge"),
        *calls("app/payments.py:20:post", "app/payments.py::charge"),
        *calls("app/payments.py:44:get", "app/payments.py::refund"),
        EdgeSpec(
            EdgeKind.DEFINED_IN, (NodeKind.SYMBOL, "app/payments.py::charge"),
            (NodeKind.FILE, "app/payments.py"), Confidence.CONFIRMED, _evidence(),
        ),
        EdgeSpec(
            EdgeKind.DEFINED_IN, (NodeKind.SYMBOL, "app/payments.py::refund"),
            (NodeKind.FILE, "app/payments.py"), Confidence.CONFIRMED, _evidence(),
        ),
        EdgeSpec(
            EdgeKind.DEFINED_IN, (NodeKind.SYMBOL, "app/webhooks.py::on_paid"),
            (NodeKind.FILE, "app/webhooks.py"), Confidence.CONFIRMED, _evidence(),
        ),
        EdgeSpec(
            EdgeKind.IMPLEMENTS_WORKFLOW, (NodeKind.SYMBOL, "app/payments.py::charge"),
            (NodeKind.WORKFLOW, "Checkout"), Confidence.INFERRED, _evidence(),
        ),
        EdgeSpec(
            EdgeKind.IMPLEMENTS_WORKFLOW, (NodeKind.SYMBOL, "app/payments.py::refund"),
            (NodeKind.WORKFLOW, "Refunds"), Confidence.INFERRED, _evidence(),
        ),
        EdgeSpec(
            EdgeKind.IMPLEMENTS_WORKFLOW, (NodeKind.SYMBOL, "app/webhooks.py::on_paid"),
            (NodeKind.WORKFLOW, "Payment Events"), Confidence.INFERRED, _evidence(),
        ),
        EdgeSpec(
            EdgeKind.COVERED_BY_TEST, (NodeKind.SYMBOL, "app/payments.py::charge"),
            (NodeKind.TEST, "tests/test_payments.py"), Confidence.CONFIRMED, _evidence(),
        ),
        EdgeSpec(
            EdgeKind.COVERED_BY_TEST, (NodeKind.SYMBOL, "app/webhooks.py::on_paid"),
            (NodeKind.TEST, "tests/test_webhooks.py"), Confidence.CONFIRMED, _evidence(),
        ),
        # This project handles payment.paid only. `invoice.sent` is deliberately
        # absent, so a change to it must correlate to nothing.
        EdgeSpec(
            EdgeKind.HANDLES_WEBHOOK_EVENT, (NodeKind.SYMBOL, "app/webhooks.py::on_paid"),
            (NodeKind.PROVIDER, PROVIDER), Confidence.CONFIRMED, _evidence("app/webhooks.py", 8),
            {"events": ["payment.paid"]},
        ),
        EdgeSpec(
            EdgeKind.REQUIRES_PERMISSION, (NodeKind.PROVIDER, PROVIDER),
            (NodeKind.PERMISSION, f"{PROVIDER}:customers.read"),
            Confidence.CONFIRMED, _evidence(),
        ),
    ]
    return GraphDelta(nodes=nodes, edges=edges)


def _change(
    change_type: ChangeType,
    resource: str,
    *,
    breaking: bool = True,
    security_relevant: bool = False,
    authentication_relevant: bool = False,
    old_contract: dict[str, object] | None = None,
    new_contract: dict[str, object] | None = None,
) -> ProviderChange:
    return ProviderChange(
        provider_id=PROVIDER,
        old_version="v1",
        new_version="v2",
        change_type=change_type,
        resource=resource,
        old_contract=old_contract,
        new_contract=new_contract,
        breaking=breaking,
        security_relevant=security_relevant,
        authentication_relevant=authentication_relevant,
        source=SPEC_SOURCE,
        evidence=Evidence(
            kind=EvidenceKind.PROVIDER_SPEC,
            confidence=Confidence.CONFIRMED,
            source_ref=SPEC_SOURCE,
            excerpt="fixture",
        ),
    )


def change_set() -> list[ProviderChange]:
    """A realistic release: mostly irrelevant to this project.

    Nine of the twelve changes touch nothing here. That ratio is the point —
    C6-03's acceptance is that the majority reach `CHANGE_IRRELEVANT` and open
    no migration run.
    """
    return [
        # --- reaches this project ---------------------------------------
        _change(ChangeType.REQUEST_FIELD_REQUIRED, "POST /v1/charges request.currency"),
        _change(ChangeType.WEBHOOK_EVENT_CHANGED, "payment.paid"),
        _change(
            ChangeType.RESPONSE_FIELD_REMOVED, "GET /v1/refunds/{id} response.amount"
        ),
        # --- touches nothing here ---------------------------------------
        _change(ChangeType.ENDPOINT_REMOVED, "POST /v1/subscriptions"),
        _change(ChangeType.ENDPOINT_ADDED, "GET /v2/disputes", breaking=False),
        _change(ChangeType.WEBHOOK_EVENT_CHANGED, "invoice.sent"),
        _change(ChangeType.REQUEST_FIELD_ADDED, "POST /v1/payouts request.note", breaking=False),
        _change(ChangeType.RESPONSE_SHAPE_CHANGED, "GET /v1/balance response.pending"),
        _change(ChangeType.HEADER_REQUIREMENT_CHANGED, "POST /v1/transfers"),
        _change(ChangeType.ERROR_CONTRACT_CHANGED, "GET /v1/events"),
        _change(
            ChangeType.ENUM_CHANGED,
            "PayoutStatus",
            old_contract={"values": ["scheduled", "sent"]},
            new_contract={"values": ["scheduled", "sent", "returned"]},
            breaking=False,
        ),
        _change(ChangeType.DOCUMENTATION_ONLY, "refund examples", breaking=False),
    ]


#: The committed expected mapping (C6-02 acceptance). Resource -> the call site
#: and webhook handler keys it must correlate to, and nothing else.
EXPECTED_CORRELATION: dict[str, dict[str, list[str]]] = {
    "POST /v1/charges request.currency": {
        "call_sites": ["app/payments.py:12:post", "app/payments.py:20:post"],
        "webhook_handlers": [],
    },
    "payment.paid": {
        "call_sites": [],
        "webhook_handlers": ["app/webhooks.py::on_paid"],
    },
    "GET /v1/refunds/{id} response.amount": {
        "call_sites": ["app/payments.py:44:get"],
        "webhook_handlers": [],
    },
    "POST /v1/subscriptions": {"call_sites": [], "webhook_handlers": []},
    "GET /v2/disputes": {"call_sites": [], "webhook_handlers": []},
    "invoice.sent": {"call_sites": [], "webhook_handlers": []},
    "POST /v1/payouts request.note": {"call_sites": [], "webhook_handlers": []},
    "GET /v1/balance response.pending": {"call_sites": [], "webhook_handlers": []},
    "POST /v1/transfers": {"call_sites": [], "webhook_handlers": []},
    "GET /v1/events": {"call_sites": [], "webhook_handlers": []},
    "PayoutStatus": {"call_sites": [], "webhook_handlers": []},
    "refund examples": {"call_sites": [], "webhook_handlers": []},
}
