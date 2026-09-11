"""C5-03: the deterministic spec differ.

No model runs here, and that is the point of the ticket — comparing two JSON
documents is something ordinary code does exactly and a model does
approximately. Every assertion below is reproducible byte-for-byte.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.models.enums import ChangeType, Confidence, EvidenceKind, SourceKind
from backend.models.schemas import ProviderChange, SourceRef
from backend.providers.diff import (
    RENAME_SIMILARITY_THRESHOLD,
    MalformedSpec,
    SpecDiffer,
    _similarity,
    operations,
    parse_spec,
)
from tests.support.provider_fixtures import spec_v1, spec_v2

SOURCE = SourceRef(
    kind=SourceKind.OPENAPI_SPEC,
    url="https://acmepay.test/openapi/v2.json",
    retrieved_at=None,
)

#: Every change type the differ is allowed to emit, taken from the authoritative
#: partition rather than re-derived by exclusion — `tests/unit/models/test_enums.py`
#: is what keeps the two halves a partition, so this list cannot quietly drift
#: from the one `diff.py` enforces against.
SPEC_DERIVABLE = sorted(ChangeType.spec_derivable(), key=lambda change: change.value)


def _differ(**kwargs: Any) -> SpecDiffer:
    return SpecDiffer("acmepay", "v1", "v2", SOURCE, **kwargs)


@pytest.fixture(scope="module")
def fixture_changes() -> list[ProviderChange]:
    return _differ().diff(json.dumps(spec_v1()), json.dumps(spec_v2()))


def test_there_are_spec_derivable_types_to_cover() -> None:
    """Guards the parametrized test below against silently covering nothing."""
    assert len(SPEC_DERIVABLE) >= 12


@pytest.mark.parametrize("change_type", SPEC_DERIVABLE, ids=lambda c: c.value)
def test_each_spec_derivable_change_type_has_a_fixture(
    change_type: ChangeType, fixture_changes: list[ProviderChange]
) -> None:
    """C5-03 acceptance: one fixture per spec-derivable change type.

    A single v1/v2 pair carries one instance of every kind, so the fixture is a
    realistic provider release rather than fourteen synthetic one-field specs
    that would never co-occur.
    """
    matching = [c for c in fixture_changes if c.change_type is change_type]

    assert matching, f"the fixture pair produces no {change_type.value}"


def test_the_differ_emits_nothing_but_spec_derivable_types(
    fixture_changes: list[ProviderChange],
) -> None:
    emitted = {c.change_type for c in fixture_changes}

    assert not emitted & ChangeType.changelog_derived()


def test_a_changelog_derived_type_cannot_be_emitted_even_by_mistake() -> None:
    """The boundary is enforced inside `_change`, not only by convention.

    `02_ARCHITECTURE.md` §10 splits deterministic extraction from model
    interpretation. A future edit that wired a rate-limit heuristic into the
    differ would blur that line, and this makes it fail loudly rather than
    quietly producing an unattributable "confirmed" change.
    """
    differ = _differ()

    for change_type in sorted(ChangeType.changelog_derived(), key=lambda c: c.value):
        with pytest.raises(AssertionError, match="changelog-derived"):
            differ._change(change_type, resource="anything", breaking=True)


def test_every_change_is_confirmed_and_attributable(
    fixture_changes: list[ProviderChange],
) -> None:
    """Deterministic extraction produces CONFIRMED, always with a source."""
    assert fixture_changes
    for change in fixture_changes:
        assert change.evidence.confidence is Confidence.CONFIRMED
        assert change.evidence.kind is EvidenceKind.PROVIDER_SPEC
        assert change.source.url == SOURCE.url


# --- breaking and security classification --------------------------------


@pytest.mark.parametrize(
    ("change_type", "resource", "breaking"),
    [
        (ChangeType.ENDPOINT_ADDED, "GET /v2/disputes", False),
        (ChangeType.ENDPOINT_REMOVED, "POST /v1/refunds", True),
        (ChangeType.ENDPOINT_RENAMED, "POST /v1/charges", True),
        (ChangeType.REQUEST_FIELD_ADDED, "POST /v2/charges request.description", False),
        (ChangeType.REQUEST_FIELD_REMOVED, "POST /v2/charges request.note", True),
        (ChangeType.REQUEST_FIELD_REQUIRED, "POST /v2/charges request.currency", True),
        (ChangeType.RESPONSE_FIELD_REMOVED, "POST /v2/charges response.amount", True),
        (ChangeType.RESPONSE_SHAPE_CHANGED, "POST /v2/charges response.status", True),
    ],
)
def test_breaking_flag_matches_caller_consequence(
    change_type: ChangeType,
    resource: str,
    breaking: bool,
    fixture_changes: list[ProviderChange],
) -> None:
    """An added optional field breaks nobody; a newly required one breaks everyone.

    This distinction is what stops Continuity opening a migration PR for every
    additive provider release.
    """
    matching = [
        c for c in fixture_changes if c.change_type is change_type and c.resource == resource
    ]

    assert len(matching) == 1, f"expected exactly one {change_type.value} on {resource}"
    assert matching[0].breaking is breaking


@pytest.mark.parametrize(
    "change_type",
    [ChangeType.AUTHENTICATION_CHANGED, ChangeType.OAUTH_SCOPE_CHANGED],
)
def test_auth_changes_are_flagged_security_relevant(
    change_type: ChangeType, fixture_changes: list[ProviderChange]
) -> None:
    """These are the changes the approval gate exists for."""
    matching = [c for c in fixture_changes if c.change_type is change_type]

    assert matching
    for change in matching:
        assert change.security_relevant
        assert change.authentication_relevant


def test_a_widened_scope_is_recorded_without_being_called_breaking(
    fixture_changes: list[ProviderChange],
) -> None:
    """v2 adds `customers.write`; nothing existing stops working.

    It is still security-relevant, which is the flag that routes it to a human.
    """
    (scope_change,) = [
        c for c in fixture_changes if c.change_type is ChangeType.OAUTH_SCOPE_CHANGED
    ]

    assert scope_change.breaking is False
    assert scope_change.security_relevant is True
    assert scope_change.new_contract == {
        "scopes": ["customers.read", "customers.write"]
    }


# --- rename detection ----------------------------------------------------


def _rename_pair(new_path: str) -> tuple[str, str]:
    body = {
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": {"amount": {"type": "integer"}}}
            }
        }
    }
    old = {"paths": {"/v1/charges": {"post": {"requestBody": body, "responses": {"200": {}}}}}}
    new = {"paths": {new_path: {"post": {"requestBody": body, "responses": {"200": {}}}}}}
    return json.dumps(old), json.dumps(new)


def test_a_close_pair_is_reported_as_a_rename() -> None:
    old, new = _rename_pair("/v2/charges")
    similarity = _similarity(
        operations(json.loads(old))["POST /v1/charges"],
        operations(json.loads(new))["POST /v2/charges"],
    )
    assert similarity >= RENAME_SIMILARITY_THRESHOLD, "fixture no longer clears the threshold"

    changes = _differ().diff(old, new)

    assert [c.change_type for c in changes] == [ChangeType.ENDPOINT_RENAMED]
    assert changes[0].new_contract == {"operation": "POST /v2/charges"}


def test_a_distant_pair_is_reported_honestly_as_removal_and_addition() -> None:
    """Below the threshold, two changes — never an invented mapping.

    A false rename is worse than an honest pair: it sends a migration looking
    for a correspondence that does not exist, and the resulting patch edits call
    sites to point at an endpoint that was never their replacement.
    """
    old, new = _rename_pair("/v1/subscription_schedules")
    similarity = _similarity(
        operations(json.loads(old))["POST /v1/charges"],
        operations(json.loads(new))["POST /v1/subscription_schedules"],
    )
    assert similarity < RENAME_SIMILARITY_THRESHOLD, "fixture no longer sits below the threshold"

    changes = _differ().diff(old, new)

    assert {c.change_type for c in changes} == {
        ChangeType.ENDPOINT_REMOVED,
        ChangeType.ENDPOINT_ADDED,
    }
    assert not any(c.change_type is ChangeType.ENDPOINT_RENAMED for c in changes)


def test_the_threshold_is_the_thing_that_decides() -> None:
    """The same pair, either side of the boundary.

    Asserting the threshold *governs* rather than asserting two hand-picked
    string pairs happen to land on opposite sides — the latter would break
    whenever `difflib`'s ratio shifted, for no real reason.
    """
    old, new = _rename_pair("/v1/subscription_schedules")
    score = _similarity(
        operations(json.loads(old))["POST /v1/charges"],
        operations(json.loads(new))["POST /v1/subscription_schedules"],
    )

    permissive = _differ(rename_threshold=score - 0.01).diff(old, new)
    strict = _differ(rename_threshold=score + 0.01).diff(old, new)

    assert [c.change_type for c in permissive] == [ChangeType.ENDPOINT_RENAMED]
    assert ChangeType.ENDPOINT_RENAMED not in {c.change_type for c in strict}


def test_a_rename_does_not_hide_the_field_changes_that_came_with_it() -> None:
    """Regression: a renamed endpoint is still the same endpoint.

    The renamed pair leaves `removed`/`added` before the intersection loop runs,
    so an earlier version reported only `endpoint_renamed` and dropped every
    field-level change on that operation. A provider that renames a path *and*
    makes a field required would have had the path migrated and the required
    field missed — a patch that compiles, ships, and 400s in production.
    """
    old = {
        "paths": {
            "/v1/charges": {
                "post": {
                    "parameters": [{"name": "X-Key", "in": "header", "required": True}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["amount"],
                                    "properties": {
                                        "amount": {"type": "integer"},
                                        "currency": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {"200": {}},
                }
            }
        }
    }
    new = json.loads(json.dumps(old).replace("/v1/charges", "/v2/charges"))
    operation = new["paths"]["/v2/charges"]["post"]
    operation["requestBody"]["content"]["application/json"]["schema"]["required"] = [
        "amount",
        "currency",
    ]
    operation["parameters"].append(
        {"name": "X-Api-Version", "in": "header", "required": True}
    )

    changes = _differ().diff(json.dumps(old), json.dumps(new))
    by_type = {c.change_type: c for c in changes}

    assert ChangeType.ENDPOINT_RENAMED in by_type
    assert ChangeType.REQUEST_FIELD_REQUIRED in by_type
    assert ChangeType.HEADER_REQUIREMENT_CHANGED in by_type
    # Keyed to the endpoint callers must now satisfy, not the one that is gone.
    assert by_type[ChangeType.REQUEST_FIELD_REQUIRED].resource == (
        "POST /v2/charges request.currency"
    )


def test_a_rename_is_not_claimed_across_different_methods() -> None:
    old = json.dumps({"paths": {"/v1/charges": {"post": {"responses": {"200": {}}}}}})
    new = json.dumps({"paths": {"/v1/charges2": {"get": {"responses": {"200": {}}}}}})

    changes = _differ().diff(old, new)

    assert ChangeType.ENDPOINT_RENAMED not in {c.change_type for c in changes}


def test_one_new_endpoint_cannot_be_claimed_as_two_renames() -> None:
    old = json.dumps(
        {
            "paths": {
                "/v1/charges": {"post": {"responses": {"200": {}}}},
                "/v1/charge": {"post": {"responses": {"200": {}}}},
            }
        }
    )
    new = json.dumps({"paths": {"/v1/charged": {"post": {"responses": {"200": {}}}}}})

    changes = _differ().diff(old, new)
    renames = [c for c in changes if c.change_type is ChangeType.ENDPOINT_RENAMED]

    assert len(renames) <= 1


# --- a newly added field that is required --------------------------------


def test_a_newly_added_required_field_is_breaking_not_merely_added() -> None:
    """The other route into REQUEST_FIELD_REQUIRED.

    The fixture covers a field that *became* required. A field that arrives
    already required is the same break for callers and must not be softened into
    `request_field_added`.
    """

    def spec(properties: dict[str, Any], required: list[str]) -> str:
        return json.dumps(
            {
                "paths": {
                    "/v1/charges": {
                        "post": {
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": required,
                                            "properties": properties,
                                        }
                                    }
                                }
                            },
                            "responses": {"200": {}},
                        }
                    }
                }
            }
        )

    changes = _differ().diff(
        spec({"amount": {"type": "integer"}}, ["amount"]),
        spec(
            {"amount": {"type": "integer"}, "idempotency_key": {"type": "string"}},
            ["amount", "idempotency_key"],
        ),
    )

    (change,) = changes
    assert change.change_type is ChangeType.REQUEST_FIELD_REQUIRED
    assert change.breaking is True


# --- parsing and failure modes -------------------------------------------


def test_yaml_specs_are_accepted() -> None:
    """Providers publish both; the differ must not care which."""
    parsed = parse_spec("openapi: 3.1.0\npaths:\n  /v1/charges:\n    post:\n      responses:\n        '200': {}\n")

    assert parsed["openapi"] == "3.1.0"
    assert "/v1/charges" in parsed["paths"]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("", "empty"),
        ("   \n  ", "whitespace only"),
        ("[1, 2, 3]", "not an object"),
        ("{ this is not: valid: json or yaml ][", "unparseable"),
        ("<html><body>404 Not Found</body></html>", "an error page, not a spec"),
    ],
    ids=["empty", "whitespace", "not-an-object", "unparseable", "error-page"],
)
def test_a_malformed_spec_fails_closed(content: str, reason: str) -> None:
    """Never a partial diff.

    A half-parsed document reports its unreadable endpoints as *removed*, and a
    migration would then rewrite call sites for endpoints that never went away.
    The HTML case is the realistic one: a provider serving an error page where a
    spec used to be.
    """
    with pytest.raises(MalformedSpec):
        parse_spec(content)

    with pytest.raises(MalformedSpec):
        _differ().diff(json.dumps(spec_v1()), content)


def test_an_identical_spec_produces_no_changes() -> None:
    content = json.dumps(spec_v1())

    assert _differ().diff(content, content) == []


def test_key_order_and_whitespace_do_not_produce_changes() -> None:
    """A reserialized spec is the same spec.

    Providers regenerate specs constantly. If formatting alone produced changes,
    every poll would open a migration.
    """
    original = json.dumps(spec_v1(), sort_keys=True)
    reformatted = json.dumps(json.loads(original), indent=4, sort_keys=False)

    assert _differ().diff(original, reformatted) == []


def test_a_spec_with_no_paths_block_is_diffed_rather_than_crashing() -> None:
    """A document missing `paths` is a valid document, not a parse failure."""
    changes = _differ().diff(json.dumps({"openapi": "3.1.0"}), json.dumps(spec_v1()))
    emitted = {c.change_type for c in changes}

    # Every operation in v1 arrives, and so do the auth and scope blocks it
    # declares — all three are real differences from an empty document.
    assert ChangeType.ENDPOINT_ADDED in emitted
    assert emitted <= {
        ChangeType.ENDPOINT_ADDED,
        ChangeType.AUTHENTICATION_CHANGED,
        ChangeType.OAUTH_SCOPE_CHANGED,
        ChangeType.WEBHOOK_EVENT_CHANGED,
    }
    assert len([c for c in changes if c.change_type is ChangeType.ENDPOINT_ADDED]) == 2
