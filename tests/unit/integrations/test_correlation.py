"""C6-02: deterministic change-to-graph correlation.

No model is involved, so every assertion here is exact rather than
approximate — the expected mapping is committed in
`tests/support/correlation_fixtures.py` and compared whole.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from backend.integrations.correlation import (
    build_rename_map,
    correlate,
    correlate_all,
    parse_endpoint,
    paths_match,
)
from backend.integrations.graph import IntegrationGraph
from backend.models import Project, Repository, User
from backend.models.enums import ChangeType
from backend.models.session import session_scope
from tests.support.correlation_fixtures import (
    EXPECTED_CORRELATION,
    PROVIDER,
    _change,
    change_set,
    fixture_graph,
)

pytestmark = pytest.mark.usefixtures("database")

REPO_ROOT = Path(__file__).resolve().parents[3]


async def _project_with_graph() -> tuple[Project, int]:
    async with session_scope() as session:
        user = User(google_subject=f"s-{uuid.uuid4()}", email="c@example.test")
        session.add(user)
        await session.flush()
        repository = Repository(owner="acme", name=f"r-{uuid.uuid4().hex[:8]}")
        session.add(repository)
        await session.flush()
        project = Project(user_id=user.id, repository_id=repository.id, name="p")
        session.add(project)
        await session.flush()

        graph = IntegrationGraph(session, project.id)
        version = await graph.next_version()
        await graph.apply(fixture_graph(), version=version)
        await session.refresh(project)
        return project, version


# --- path matching -------------------------------------------------------


@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        ("POST /v1/charges", ("POST", "/v1/charges")),
        ("GET /v1/refunds/{id} response.amount", ("GET", "/v1/refunds/{id}")),
        ("DELETE /v2/x request.y", ("DELETE", "/v2/x")),
        ("payment.paid", None),
        ("oauth.scopes", None),
        ("security", None),
        ("ChargeStatus", None),
    ],
)
def test_endpoint_parsing(resource: str, expected: tuple[str, str] | None) -> None:
    assert parse_endpoint(resource) == expected


@pytest.mark.parametrize(
    ("left", "right", "matches"),
    [
        ("/v1/charges", "/v1/charges", True),
        ("/v1/charges", "/v1/charges/", True),
        ("v1/charges", "/v1/charges", True),
        ("/v1/charges", "/V1/Charges", True),
        ("/v1/refunds/{id}", "/v1/refunds/re_123", True),
        ("/v1/refunds/:id", "/v1/refunds/re_123", True),
        # Different arity is a different endpoint.
        ("/v1/charges", "/v1/charges/refunds", False),
        ("/v1/charges/{id}", "/v1/charges", False),
        ("/v1/charges", "/v1/payouts", False),
        ("/v1/charges", "/v2/charges", False),
    ],
)
def test_path_matching(left: str, right: str, matches: bool) -> None:
    """Segment-wise, so a template matches a value and arity is respected.

    `/v1/charges` matching `/v1/charges/refunds` would send a migration at a
    call site the change never touched.
    """
    assert paths_match(left, right) is matches
    assert paths_match(right, left) is matches


# --- the committed mapping -----------------------------------------------


async def test_correlation_equals_the_committed_expected_mapping() -> None:
    """C6-02 acceptance, compared whole rather than spot-checked.

    Comparing the entire mapping in one assertion is deliberate: a per-change
    loop that asserted only the non-empty cases would not notice correlation
    growing a false positive on one of the nine changes that must reach nothing.
    """
    project, version = await _project_with_graph()
    changes = change_set()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlations = await correlate_all(graph, version, PROVIDER, changes)

    actual = {
        correlation.change.resource: {
            "call_sites": sorted(node.key for node in correlation.call_sites),
            "webhook_handlers": sorted(node.key for node in correlation.webhook_handlers),
        }
        for correlation in correlations
    }

    assert actual == EXPECTED_CORRELATION


async def test_most_of_a_real_release_touches_nothing() -> None:
    """The property that makes autonomous monitoring tolerable.

    If correlation were loose, every provider release would reach some code and
    Continuity would open a pull request every week.
    """
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlations = await correlate_all(graph, version, PROVIDER, change_set())

    reaching = [c for c in correlations if not c.is_empty]

    assert len(reaching) == 3
    assert len(correlations) == 12


async def test_a_change_touching_nothing_is_empty_without_error_or_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C6-02 acceptance, stated as the ticket does.

    Not an error, and not a warning either: this is the normal outcome, and a
    warning on the normal outcome is how logs become unreadable.
    """
    project, version = await _project_with_graph()
    change = _change(ChangeType.ENDPOINT_REMOVED, "POST /v1/nothing_here")

    with caplog.at_level("INFO"):
        async with session_scope() as session:
            graph = IntegrationGraph(session, project.id)
            correlation = await correlate(graph, version, PROVIDER, change)

    assert correlation.is_empty
    assert correlation.call_sites == []
    assert correlation.blast is None
    assert correlation.seed_ids == set()
    assert [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}] == []


async def test_an_unknown_provider_correlates_to_nothing() -> None:
    """A project that does not use this provider at all."""
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlation = await correlate(
            graph,
            version,
            "some-other-provider",
            _change(ChangeType.ENDPOINT_REMOVED, "POST /v1/charges"),
        )

    assert correlation.is_empty


# --- what correlation reaches --------------------------------------------


async def test_a_correlated_change_carries_its_blast_radius() -> None:
    """Correlation answers "what code", the blast radius answers "what then".

    Two call sites in `charge()` reach one symbol, one file, the Checkout
    workflow, and the test that covers it — which is the chain that turns "a
    field became required" into something a person can act on.
    """
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlation = await correlate(
            graph,
            version,
            PROVIDER,
            _change(ChangeType.REQUEST_FIELD_REQUIRED, "POST /v1/charges request.currency"),
        )

    assert correlation.blast is not None
    summary = correlation.blast.summary()
    assert summary["workflows"] == ["Checkout"]
    assert summary["tests"] == ["tests/test_payments.py"]
    assert summary["files"] == ["app/payments.py"]
    assert correlation.basis == ["calls /v1/charges"]


async def test_a_webhook_change_reaches_only_the_handler_for_that_event() -> None:
    """`payment.paid` changing does not affect the handler for `invoice.sent`.

    Correlating every handler for the provider would be easier and would put
    unrelated code into a migration diff.
    """
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        handled = await correlate(
            graph, version, PROVIDER, _change(ChangeType.WEBHOOK_EVENT_CHANGED, "payment.paid")
        )
        unhandled = await correlate(
            graph, version, PROVIDER, _change(ChangeType.WEBHOOK_EVENT_CHANGED, "invoice.sent")
        )

    assert [n.key for n in handled.webhook_handlers] == ["app/webhooks.py::on_paid"]
    assert handled.blast is not None
    assert handled.blast.summary()["tests"] == ["tests/test_webhooks.py"]
    assert unhandled.is_empty


async def test_an_authentication_change_reaches_every_call_site() -> None:
    """Every call authenticates, so an auth change reaches all of them.

    Over-reaching here is correct where it would be wrong elsewhere: a changed
    auth scheme genuinely does affect each call, and missing one would ship a
    half-migrated client.
    """
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlation = await correlate(
            graph,
            version,
            PROVIDER,
            _change(
                ChangeType.AUTHENTICATION_CHANGED,
                "security",
                security_relevant=True,
                authentication_relevant=True,
            ),
        )

    assert len(correlation.call_sites) == 3
    assert [n.label for n in correlation.permissions] == ["customers.read"]


async def test_a_widened_scope_reaches_permissions_but_not_every_call_site() -> None:
    """Adding a scope breaks no existing call.

    Treating it like a breaking auth change would drag every call site into a
    migration for a change that requires no code edit at all.
    """
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlation = await correlate(
            graph,
            version,
            PROVIDER,
            _change(
                ChangeType.OAUTH_SCOPE_CHANGED,
                "oauth.scopes",
                breaking=False,
                security_relevant=True,
                old_contract={"scopes": ["customers.read"]},
                new_contract={"scopes": ["customers.read", "customers.write"]},
            ),
        )

    assert [n.label for n in correlation.permissions] == ["customers.read"]
    assert correlation.call_sites == []


async def test_a_removed_scope_reaches_the_call_sites_too() -> None:
    """Removal is breaking, and every call that needed it now fails."""
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlation = await correlate(
            graph,
            version,
            PROVIDER,
            _change(
                ChangeType.OAUTH_SCOPE_CHANGED,
                "oauth.scopes",
                breaking=True,
                security_relevant=True,
                old_contract={"scopes": ["customers.read"]},
                new_contract={"scopes": []},
            ),
        )

    assert len(correlation.call_sites) == 3
    assert [n.label for n in correlation.permissions] == ["customers.read"]


async def test_an_enum_change_reaches_only_call_sites_passing_a_removed_value() -> None:
    """Additive enum changes break nobody.

    The fixture has a call site passing `status=pending` and one passing
    `status=paid`; removing `pending` must reach exactly the first.
    """
    project, version = await _project_with_graph()

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        removed = await correlate(
            graph, version, PROVIDER,
            _change(
                ChangeType.ENUM_CHANGED, "ChargeStatus",
                old_contract={"values": ["pending", "paid"]},
                new_contract={"values": ["paid"]},
            ),
        )
        added = await correlate(
            graph, version, PROVIDER,
            _change(
                ChangeType.ENUM_CHANGED, "ChargeStatus", breaking=False,
                old_contract={"values": ["pending", "paid"]},
                new_contract={"values": ["pending", "paid", "failed"]},
            ),
        )

    assert [n.key for n in removed.call_sites] == ["app/payments.py:12:post"]
    assert added.is_empty


# --- renames -------------------------------------------------------------


def test_a_rename_map_is_built_from_the_change_set() -> None:
    changes = [
        _change(
            ChangeType.ENDPOINT_RENAMED,
            "POST /v1/charges",
            old_contract={"operation": "POST /v1/charges"},
            new_contract={"operation": "POST /v2/charges"},
        )
    ]

    assert build_rename_map(changes) == {"/v2/charges": "/v1/charges"}


async def test_a_field_change_on_a_renamed_endpoint_still_finds_the_old_call_sites() -> None:
    """The interaction that would otherwise silently lose the worst changes.

    After Phase 5's rename fix, field changes on a renamed endpoint are keyed to
    the *new* path, because that is what callers must satisfy. The repository
    has not migrated, so its call sites still name the old one. Without the
    rename map, a rename-plus-required-field release would correlate the rename
    to the call sites and the required field to nothing at all.
    """
    project, version = await _project_with_graph()
    changes = [
        _change(
            ChangeType.ENDPOINT_RENAMED,
            "POST /v1/charges",
            old_contract={"operation": "POST /v1/charges"},
            new_contract={"operation": "POST /v2/charges"},
        ),
        _change(ChangeType.REQUEST_FIELD_REQUIRED, "POST /v2/charges request.currency"),
    ]

    async with session_scope() as session:
        graph = IntegrationGraph(session, project.id)
        correlations = await correlate_all(graph, version, PROVIDER, changes)
        # Correlated one at a time, with no shared map, the field change is lost.
        without_map = await correlate(graph, version, PROVIDER, changes[1])

    rename, field_change = correlations
    assert sorted(n.key for n in rename.call_sites) == [
        "app/payments.py:12:post",
        "app/payments.py:20:post",
    ]
    assert sorted(n.key for n in field_change.call_sites) == [
        "app/payments.py:12:post",
        "app/payments.py:20:post",
    ]
    assert without_map.is_empty


# --- no model ------------------------------------------------------------


def test_correlation_invokes_no_model() -> None:
    """C6-02 acceptance, asserted structurally.

    Behavioural proof would only cover the paths a test happens to exercise.
    This holds for code nobody has written yet.
    """
    source = (REPO_ROOT / "backend" / "integrations" / "correlation.py").read_text()
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    offending = [
        module
        for module in imported
        if module.startswith(("strands", "backend.agents", "backend.shared.model_provider"))
    ]
    assert not offending, f"correlation reaches a model: {offending}"
