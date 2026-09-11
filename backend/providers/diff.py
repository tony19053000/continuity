"""Deterministic OpenAPI/JSON-schema diff.

**No model is involved.** Comparing two JSON documents is something ordinary code
does exactly and a model does approximately, so the change set comes from here
and the Change Scout only adds what prose reveals
(`02_ARCHITECTURE.md` §10, division of labour).

This module emits **only** the spec-derivable subset of `ChangeType`. The
changelog-derived members — `rate_limit_changed`, `sdk_deprecated`,
`api_version_deprecated`, `documentation_only` — have no reliable schema
representation and belong to C5-04. A test asserts the differ never emits one,
so the boundary cannot erode.

Malformed input fails closed: a spec that will not parse raises rather than
producing a change set from a partial document, because a partial diff would
report endpoints as *removed* when they were merely unreadable.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from typing import Any, Final

from backend.models.enums import ChangeType, Confidence, EvidenceKind
from backend.models.schemas import Evidence, ProviderChange, SourceRef
from backend.shared.errors import ContinuityError

#: Path-and-shape similarity above which a removal/addition pair is reported as
#: a rename rather than two separate changes. Deliberately high: calling two
#: unrelated endpoints a rename would send a migration chasing a change that
#: never happened, which is worse than reporting the pair honestly.
RENAME_SIMILARITY_THRESHOLD: Final = 0.75

_HTTP_METHODS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: Security schemes whose change alters how callers authenticate.
_AUTH_KEYS: Final = frozenset({"security", "securitySchemes", "securityDefinitions"})


class MalformedSpec(ContinuityError):
    """A provider spec could not be parsed.

    Fails closed on purpose. Diffing a half-parsed document would report every
    unreadable endpoint as removed, and a migration would then "fix" code that
    was never broken.
    """

    code = "malformed_provider_spec"
    status_code = 422
    message = "The provider specification could not be parsed."


@dataclass(frozen=True, slots=True)
class Operation:
    """One HTTP operation in a spec."""

    path: str
    method: str
    definition: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"


def parse_spec(content: str) -> dict[str, Any]:
    """Parse an OpenAPI document from JSON or YAML."""
    text = content.strip()
    if not text:
        raise MalformedSpec("The specification is empty.")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml

            parsed = yaml.safe_load(text)
        except Exception as exc:
            raise MalformedSpec(f"Not valid JSON or YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise MalformedSpec("The specification is not an object.")
    return parsed


def operations(spec: dict[str, Any]) -> dict[str, Operation]:
    """Every operation, keyed by `METHOD /path`."""
    found: dict[str, Operation] = {}
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return found

    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, definition in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(definition, dict):
                operation = Operation(path=str(path), method=method.lower(), definition=definition)
                found[operation.key] = operation
    return found


def _request_fields(operation: Operation) -> dict[str, dict[str, Any]]:
    """Request body properties, flattened by name."""
    body = operation.definition.get("requestBody")
    if not isinstance(body, dict):
        return {}

    for media in (body.get("content") or {}).values():
        schema = media.get("schema") if isinstance(media, dict) else None
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                required = set(schema.get("required") or [])
                return {
                    name: {**definition, "__required": name in required}
                    for name, definition in properties.items()
                    if isinstance(definition, dict)
                }
    return {}


def _response_fields(operation: Operation) -> dict[str, dict[str, Any]]:
    """Success-response properties, flattened by name."""
    responses = operation.definition.get("responses")
    if not isinstance(responses, dict):
        return {}

    for status, response in responses.items():
        if not str(status).startswith("2") or not isinstance(response, dict):
            continue
        for media in (response.get("content") or {}).values():
            schema = media.get("schema") if isinstance(media, dict) else None
            if isinstance(schema, dict):
                properties = schema.get("properties")
                if isinstance(properties, dict):
                    return {
                        name: definition
                        for name, definition in properties.items()
                        if isinstance(definition, dict)
                    }
    return {}


def _required_headers(operation: Operation) -> set[str]:
    return {
        str(parameter.get("name"))
        for parameter in operation.definition.get("parameters", []) or []
        if isinstance(parameter, dict)
        and parameter.get("in") == "header"
        and parameter.get("required")
    }


def _error_statuses(operation: Operation) -> set[str]:
    responses = operation.definition.get("responses")
    if not isinstance(responses, dict):
        return set()
    return {str(status) for status in responses if not str(status).startswith("2")}


def _scopes(spec: dict[str, Any], operation: Operation | None = None) -> set[str]:
    """OAuth scopes, whether declared globally or per-operation."""
    scopes: set[str] = set()

    def collect(requirements: Any) -> None:
        if not isinstance(requirements, list):
            return
        for requirement in requirements:
            if isinstance(requirement, dict):
                for values in requirement.values():
                    if isinstance(values, list):
                        scopes.update(str(value) for value in values)

    collect(spec.get("security"))
    if operation is not None:
        collect(operation.definition.get("security"))
    return scopes


def _auth_shape(spec: dict[str, Any]) -> str:
    """A stable rendering of the spec's authentication configuration."""
    components = spec.get("components") or {}
    payload = {
        key: value
        for key, value in {**spec, **components}.items()
        if key in _AUTH_KEYS
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _webhook_events(spec: dict[str, Any]) -> set[str]:
    """Event names from a `webhooks` block or an events enum."""
    events: set[str] = set()

    webhooks = spec.get("webhooks")
    if isinstance(webhooks, dict):
        events.update(str(name) for name in webhooks)

    components = spec.get("components") or {}
    schemas = components.get("schemas") or {}
    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        if "event" not in str(name).lower():
            continue
        properties = schema.get("properties") or {}
        type_field = properties.get("type") if isinstance(properties, dict) else None
        if isinstance(type_field, dict) and isinstance(type_field.get("enum"), list):
            events.update(str(value) for value in type_field["enum"])
    return events


def _enums(spec: dict[str, Any]) -> dict[str, list[str]]:
    """Every named enum in components, for value-level comparison."""
    found: dict[str, list[str]] = {}
    schemas = (spec.get("components") or {}).get("schemas") or {}
    if not isinstance(schemas, dict):
        return found

    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        if isinstance(schema.get("enum"), list):
            found[str(name)] = [str(value) for value in schema["enum"]]
        for property_name, definition in (schema.get("properties") or {}).items():
            if isinstance(definition, dict) and isinstance(definition.get("enum"), list):
                found[f"{name}.{property_name}"] = [
                    str(value) for value in definition["enum"]
                ]
    return found


def _similarity(left: Operation, right: Operation) -> float:
    """How alike two operations are, by path and request shape.

    Both halves matter: two endpoints with similar paths but different bodies
    are different endpoints, and the point of the threshold is to avoid
    inventing a rename that never happened.
    """
    path_score = difflib.SequenceMatcher(None, left.path, right.path).ratio()
    left_fields = set(_request_fields(left))
    right_fields = set(_request_fields(right))

    if not left_fields and not right_fields:
        shape_score = 1.0
    else:
        union = left_fields | right_fields
        shape_score = len(left_fields & right_fields) / len(union) if union else 1.0

    return (path_score * 0.6) + (shape_score * 0.4)


class SpecDiffer:
    """Compares two provider specifications."""

    def __init__(
        self,
        provider_id: str,
        old_version: str,
        new_version: str,
        source: SourceRef,
        *,
        rename_threshold: float = RENAME_SIMILARITY_THRESHOLD,
    ) -> None:
        self._provider_id = provider_id
        self._old_version = old_version
        self._new_version = new_version
        self._source = source
        self._rename_threshold = rename_threshold

    def diff(self, old_content: str, new_content: str) -> list[ProviderChange]:
        old_spec = parse_spec(old_content)
        new_spec = parse_spec(new_content)

        changes: list[ProviderChange] = []
        old_ops = operations(old_spec)
        new_ops = operations(new_spec)

        removed = {key: op for key, op in old_ops.items() if key not in new_ops}
        added = {key: op for key, op in new_ops.items() if key not in old_ops}

        renames = self._detect_renames(removed, added)
        for old_key, new_key in renames.items():
            changes.append(
                self._change(
                    ChangeType.ENDPOINT_RENAMED,
                    resource=old_key,
                    breaking=True,
                    old_contract={"operation": old_key},
                    new_contract={"operation": new_key},
                )
            )
            # A renamed endpoint is still the same endpoint, so its contract has
            # to be compared too. Without this the pair leaves `removed`/`added`
            # and never enters the intersection loop below, and a provider that
            # renames a path *and* adds a required field to it would report only
            # the rename -- the migration would move the path and never learn the
            # body changed. Resources are keyed to the new operation, since that
            # is the one callers must now satisfy.
            changes.extend(
                self._diff_operation(
                    removed[old_key], added[new_key], resource_key=new_key
                )
            )
            removed.pop(old_key, None)
            added.pop(new_key, None)

        for key in sorted(removed):
            changes.append(
                self._change(ChangeType.ENDPOINT_REMOVED, resource=key, breaking=True)
            )
        for key in sorted(added):
            changes.append(
                self._change(ChangeType.ENDPOINT_ADDED, resource=key, breaking=False)
            )

        for key in sorted(set(old_ops) & set(new_ops)):
            changes.extend(self._diff_operation(old_ops[key], new_ops[key]))

        changes.extend(self._diff_spec_level(old_spec, new_spec))
        return changes

    def _detect_renames(
        self, removed: dict[str, Operation], added: dict[str, Operation]
    ) -> dict[str, str]:
        """Pair removals with additions that are plausibly the same endpoint.

        Below the threshold the pair stays two separate changes. Reporting an
        honest removal-plus-addition is better than inventing a rename, which
        would send a migration looking for a mapping that does not exist.
        """
        pairs: dict[str, str] = {}
        claimed: set[str] = set()

        for old_key in sorted(removed):
            best_key, best_score = None, 0.0
            for new_key in sorted(added):
                if new_key in claimed:
                    continue
                if removed[old_key].method != added[new_key].method:
                    continue
                score = _similarity(removed[old_key], added[new_key])
                if score > best_score:
                    best_key, best_score = new_key, score

            if best_key is not None and best_score >= self._rename_threshold:
                pairs[old_key] = best_key
                claimed.add(best_key)

        return pairs

    def _diff_operation(
        self, old: Operation, new: Operation, *, resource_key: str | None = None
    ) -> list[ProviderChange]:
        changes: list[ProviderChange] = []
        key = resource_key or old.key

        old_request = _request_fields(old)
        new_request = _request_fields(new)

        for name in sorted(set(old_request) - set(new_request)):
            changes.append(
                self._change(
                    ChangeType.REQUEST_FIELD_REMOVED,
                    resource=f"{key} request.{name}",
                    breaking=True,
                )
            )
        for name in sorted(set(new_request) - set(old_request)):
            # A newly required field breaks every existing caller; an optional
            # one breaks nobody. The distinction is the whole point.
            newly_required = bool(new_request[name].get("__required"))
            changes.append(
                self._change(
                    ChangeType.REQUEST_FIELD_REQUIRED
                    if newly_required
                    else ChangeType.REQUEST_FIELD_ADDED,
                    resource=f"{key} request.{name}",
                    breaking=newly_required,
                )
            )
        for name in sorted(set(old_request) & set(new_request)):
            was = bool(old_request[name].get("__required"))
            now = bool(new_request[name].get("__required"))
            if now and not was:
                changes.append(
                    self._change(
                        ChangeType.REQUEST_FIELD_REQUIRED,
                        resource=f"{key} request.{name}",
                        breaking=True,
                    )
                )

        old_response = _response_fields(old)
        new_response = _response_fields(new)

        for name in sorted(set(old_response) - set(new_response)):
            changes.append(
                self._change(
                    ChangeType.RESPONSE_FIELD_REMOVED,
                    resource=f"{key} response.{name}",
                    breaking=True,
                )
            )
        for name in sorted(set(old_response) & set(new_response)):
            if old_response[name].get("type") != new_response[name].get("type"):
                changes.append(
                    self._change(
                        ChangeType.RESPONSE_SHAPE_CHANGED,
                        resource=f"{key} response.{name}",
                        breaking=True,
                        old_contract={"type": old_response[name].get("type")},
                        new_contract={"type": new_response[name].get("type")},
                    )
                )

        old_headers = _required_headers(old)
        new_headers = _required_headers(new)
        if old_headers != new_headers:
            changes.append(
                self._change(
                    ChangeType.HEADER_REQUIREMENT_CHANGED,
                    resource=key,
                    breaking=bool(new_headers - old_headers),
                    old_contract={"required_headers": sorted(old_headers)},
                    new_contract={"required_headers": sorted(new_headers)},
                )
            )

        old_errors = _error_statuses(old)
        new_errors = _error_statuses(new)
        if old_errors != new_errors:
            changes.append(
                self._change(
                    ChangeType.ERROR_CONTRACT_CHANGED,
                    resource=key,
                    breaking=bool(old_errors - new_errors),
                    old_contract={"statuses": sorted(old_errors)},
                    new_contract={"statuses": sorted(new_errors)},
                )
            )

        return changes

    def _diff_spec_level(
        self, old_spec: dict[str, Any], new_spec: dict[str, Any]
    ) -> list[ProviderChange]:
        changes: list[ProviderChange] = []

        if _auth_shape(old_spec) != _auth_shape(new_spec):
            changes.append(
                self._change(
                    ChangeType.AUTHENTICATION_CHANGED,
                    resource="security",
                    breaking=True,
                    security_relevant=True,
                    authentication_relevant=True,
                )
            )

        old_scopes = _scopes(old_spec)
        new_scopes = _scopes(new_spec)
        if old_scopes != new_scopes:
            changes.append(
                self._change(
                    ChangeType.OAUTH_SCOPE_CHANGED,
                    resource="oauth.scopes",
                    # A widened scope is the case the approval system exists for.
                    breaking=bool(old_scopes - new_scopes),
                    security_relevant=True,
                    authentication_relevant=True,
                    old_contract={"scopes": sorted(old_scopes)},
                    new_contract={"scopes": sorted(new_scopes)},
                )
            )

        old_events = _webhook_events(old_spec)
        new_events = _webhook_events(new_spec)
        for event in sorted(old_events - new_events):
            changes.append(
                self._change(
                    ChangeType.WEBHOOK_EVENT_CHANGED,
                    resource=event,
                    breaking=True,
                    old_contract={"event": event},
                    new_contract={"event": None},
                )
            )

        old_enums = _enums(old_spec)
        new_enums = _enums(new_spec)
        for name in sorted(set(old_enums) & set(new_enums)):
            if old_enums[name] != new_enums[name]:
                changes.append(
                    self._change(
                        ChangeType.ENUM_CHANGED,
                        resource=name,
                        breaking=bool(set(old_enums[name]) - set(new_enums[name])),
                        old_contract={"values": old_enums[name]},
                        new_contract={"values": new_enums[name]},
                    )
                )

        return changes

    def _change(
        self,
        change_type: ChangeType,
        *,
        resource: str,
        breaking: bool,
        security_relevant: bool = False,
        authentication_relevant: bool = False,
        old_contract: dict[str, Any] | None = None,
        new_contract: dict[str, Any] | None = None,
    ) -> ProviderChange:
        # Guards the module's contract from the inside: a changelog-derived type
        # emitted here would blur the deterministic/model boundary that
        # `02_ARCHITECTURE.md` §10 draws.
        if change_type in ChangeType.changelog_derived():
            raise AssertionError(
                f"{change_type.value} is changelog-derived and must come from the "
                "Change Scout, not the deterministic differ."
            )

        return ProviderChange(
            provider_id=self._provider_id,
            old_version=self._old_version,
            new_version=self._new_version,
            change_type=change_type,
            resource=resource,
            old_contract=old_contract,
            new_contract=new_contract,
            breaking=breaking,
            security_relevant=security_relevant,
            authentication_relevant=authentication_relevant,
            source=self._source,
            evidence=Evidence(
                kind=EvidenceKind.PROVIDER_SPEC,
                confidence=Confidence.CONFIRMED,
                source_ref=self._source,
                excerpt=f"{change_type.value} on {resource}",
            ),
        )
