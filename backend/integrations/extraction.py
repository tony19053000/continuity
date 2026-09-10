"""Deterministic integration extraction.

Turns the repository index into graph nodes and edges **with no model
involved**. Everything produced here is `CONFIRMED`: it is derived from
manifests and the AST, so it can be checked by a human against a file and a line
number.

This is the floor the Integration Mapper agent builds on. Keeping the two apart
is what lets the product distinguish "your `pyproject.toml` declares `acmepay`
and `payment_service.py:12` calls it" — a fact — from "these call sites
implement Checkout" — a judgment. Conflating them is how a static analyser
starts guessing and an agent starts being believed.

A test asserts no model provider is even touched during extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from backend.integrations.graph import EdgeSpec, GraphDelta, NodeSpec
from backend.models.enums import Confidence, EdgeKind, EvidenceKind, NodeKind
from backend.models.schemas import Evidence
from backend.repository.indexer import FileKind, IndexedFile, RepositoryIndex

#: Packages that are infrastructure rather than external service providers.
#: Without this, every project would "integrate with" pytest and ruff.
_NOT_PROVIDERS: Final[frozenset[str]] = frozenset(
    {
        "pytest", "pytest-asyncio", "ruff", "mypy", "black", "isort", "flake8",
        "coverage", "tox", "nox", "pre-commit", "setuptools", "wheel", "pip",
        "typing-extensions", "types-requests", "python-dotenv",
        "pydantic", "pydantic-settings", "sqlalchemy", "alembic", "attrs",
        "fastapi", "flask", "django", "starlette", "uvicorn", "gunicorn",
        "click", "rich", "typer", "jinja2", "pyyaml", "tomli", "packaging",
        "eslint", "prettier", "typescript", "vitest", "jest", "webpack",
        "vite", "rollup", "esbuild", "tailwindcss", "postcss", "autoprefixer",
        "react", "react-dom", "next", "vue", "svelte", "@types/node",
    }
)

#: Generic HTTP clients. They indicate an integration exists but are not
#: themselves the provider — `httpx` is how you call a provider, not one.
_HTTP_CLIENTS: Final[frozenset[str]] = frozenset(
    {"requests", "httpx", "aiohttp", "urllib3", "axios", "node-fetch", "got", "ky"}
)

#: OAuth scope and permission literals, e.g. "customers.read", "repo:status".
_SCOPE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)+$")

#: A URL path with a version segment is strong evidence of an API version.
_API_VERSION_PATTERN: Final = re.compile(r"/(v\d+(?:\.\d+)?)(?:/|$)")


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """A declared dependency that looks like an external service provider."""

    provider_id: str
    package: str
    manifest_path: str
    constraint: str | None
    detected_api_version: str | None = None


def _source_evidence(path: str, start: int | None = None, end: int | None = None) -> Evidence:
    return Evidence(
        kind=EvidenceKind.SOURCE,
        confidence=Confidence.CONFIRMED,
        file_path=path,
        line_start=start,
        line_end=end,
    )


def _manifest_evidence(path: str, line: int | None) -> Evidence:
    return Evidence(
        kind=EvidenceKind.MANIFEST,
        confidence=Confidence.CONFIRMED,
        file_path=path,
        line_start=line,
    )


def provider_candidates(index: RepositoryIndex) -> list[ProviderCandidate]:
    """Declared dependencies that plausibly name an external provider.

    A declared dependency is a fact; whether it is a *provider* is a judgment
    made here by exclusion — infrastructure and generic HTTP clients are
    filtered out, and what remains is offered as a candidate. Mapping a package
    name to a canonical provider id is the adapter registry's job in Phase 5,
    where it can be done deterministically rather than inferred.
    """
    seen: dict[str, ProviderCandidate] = {}

    for dependency in index.dependencies:
        if dependency.dev_only:
            continue
        package = dependency.name.lower()
        if package in _NOT_PROVIDERS or package in _HTTP_CLIENTS:
            continue
        if package.startswith(("@types/", "types-")):
            continue

        provider_id = package.replace("@", "").replace("/", "-")
        if provider_id not in seen:
            seen[provider_id] = ProviderCandidate(
                provider_id=provider_id,
                package=dependency.name,
                manifest_path=dependency.manifest_path,
                constraint=dependency.constraint,
                detected_api_version=_api_version_for(index, package),
            )

    return sorted(seen.values(), key=lambda candidate: candidate.provider_id)


def _api_version_for(index: RepositoryIndex, package: str) -> str | None:
    """Infer the API version in use from call-site URL literals.

    Deterministic and evidence-backed: it reads `/v1/charges` out of an actual
    argument, rather than assuming a default the project may not use.
    """
    versions: dict[str, int] = {}
    root = package.split("-")[0]

    for file in index.files.values():
        if root not in " ".join(file.imports).lower() and root not in file.path.lower():
            continue
        for call in file.call_sites:
            for argument in call.arguments_preview:
                match = _API_VERSION_PATTERN.search(argument)
                if match:
                    versions[match.group(1)] = versions.get(match.group(1), 0) + 1

    if not versions:
        return None
    return max(versions.items(), key=lambda item: item[1])[0]


def _call_targets_provider(file: IndexedFile, callee: str, package_root: str) -> bool:
    """Whether a call site plausibly targets `package_root`.

    Two signals, either sufficient: the callee names the package, or the file
    imports it and the call goes through an HTTP client.
    """
    lowered = callee.lower()
    if package_root in lowered:
        return True

    imports_provider = any(package_root in imp.lower() for imp in file.imports)
    uses_http = any(client in lowered for client in ("post", "get", "put", "patch", "delete"))
    return imports_provider and uses_http and bool(file.http_clients)


def extract(index: RepositoryIndex) -> GraphDelta:
    """Build the confirmed half of the Integration Intelligence Graph."""
    nodes: list[NodeSpec] = []
    edges: list[EdgeSpec] = []

    candidates = provider_candidates(index)

    # --- providers and their SDK packages ---
    for candidate in candidates:
        attributes: dict[str, object] = {"package": candidate.package}
        if candidate.detected_api_version:
            attributes["detected_api_version"] = candidate.detected_api_version

        nodes.append(
            NodeSpec(
                kind=NodeKind.PROVIDER,
                key=candidate.provider_id,
                label=candidate.package,
                confidence=Confidence.CONFIRMED,
                evidence=_manifest_evidence(candidate.manifest_path, None),
                attributes=attributes,
            )
        )
        nodes.append(
            NodeSpec(
                kind=NodeKind.SDK,
                key=candidate.package,
                label=candidate.package,
                confidence=Confidence.CONFIRMED,
                evidence=_manifest_evidence(candidate.manifest_path, None),
                attributes={
                    "constraint": candidate.constraint,
                    "manifest": candidate.manifest_path,
                },
            )
        )
        edges.append(
            EdgeSpec(
                kind=EdgeKind.DECLARES_SDK,
                source=(NodeKind.PROVIDER, candidate.provider_id),
                target=(NodeKind.SDK, candidate.package),
                confidence=Confidence.CONFIRMED,
                evidence=_manifest_evidence(candidate.manifest_path, None),
            )
        )

    # --- files, symbols, call sites ---
    for file in index.files.values():
        if file.kind not in (FileKind.SOURCE, FileKind.TEST):
            continue

        nodes.append(
            NodeSpec(
                kind=NodeKind.FILE,
                key=file.path,
                label=file.path,
                confidence=Confidence.CONFIRMED,
                evidence=_source_evidence(file.path),
                attributes={
                    "language": file.language.value,
                    "classification": file.kind.value,
                    "content_hash": file.content_hash,
                    # TS/JS facts are regex-derived; carried through so nothing
                    # downstream treats them as AST-grade.
                    "heuristic": file.heuristic,
                },
            )
        )

        for symbol in file.symbols:
            symbol_key = f"{file.path}::{symbol.qualified_name}"
            nodes.append(
                NodeSpec(
                    kind=NodeKind.SYMBOL,
                    key=symbol_key,
                    label=f"{symbol.qualified_name}()",
                    confidence=Confidence.CONFIRMED,
                    evidence=_source_evidence(file.path, symbol.line_start, symbol.line_end),
                    attributes={
                        "kind": symbol.kind,
                        "line_start": symbol.line_start,
                        "line_end": symbol.line_end,
                        "is_route": symbol.is_route,
                        "http_method": symbol.http_method,
                        "route_path": symbol.route_path,
                    },
                )
            )
            edges.append(
                EdgeSpec(
                    kind=EdgeKind.DEFINED_IN,
                    source=(NodeKind.SYMBOL, symbol_key),
                    target=(NodeKind.FILE, file.path),
                    confidence=Confidence.CONFIRMED,
                    evidence=_source_evidence(file.path, symbol.line_start, symbol.line_end),
                )
            )

        _extract_call_sites(file, candidates, nodes, edges)
        _extract_webhooks(file, candidates, nodes, edges)

    _extract_permissions(index, candidates, nodes, edges)
    _extract_test_coverage(index, nodes, edges)

    return GraphDelta(nodes=nodes, edges=edges)


def _extract_call_sites(
    file: IndexedFile,
    candidates: list[ProviderCandidate],
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
) -> None:
    for call in file.call_sites:
        for candidate in candidates:
            root = candidate.package.split("-")[0].lower()
            if not _call_targets_provider(file, call.callee, root):
                continue

            call_key = f"{file.path}:{call.line}:{call.callee}"
            resource = next(
                (arg for arg in call.arguments_preview if arg.startswith("/")), None
            )

            nodes.append(
                NodeSpec(
                    kind=NodeKind.CALL_SITE,
                    key=call_key,
                    label=f"{call.callee} ({resource})" if resource else call.callee,
                    confidence=Confidence.CONFIRMED,
                    evidence=_source_evidence(file.path, call.line, call.line),
                    attributes={
                        "callee": call.callee,
                        "line": call.line,
                        "resource": resource,
                        "enclosing_symbol": call.enclosing_symbol,
                        "arguments": list(call.arguments_preview),
                    },
                )
            )
            edges.append(
                EdgeSpec(
                    kind=EdgeKind.CALLS_PROVIDER,
                    source=(NodeKind.CALL_SITE, call_key),
                    target=(NodeKind.PROVIDER, candidate.provider_id),
                    confidence=Confidence.CONFIRMED,
                    evidence=_source_evidence(file.path, call.line, call.line),
                    attributes={"resource": resource},
                )
            )

            if call.enclosing_symbol:
                symbol_key = f"{file.path}::{call.enclosing_symbol}"
                edges.append(
                    EdgeSpec(
                        kind=EdgeKind.DEFINED_IN,
                        source=(NodeKind.CALL_SITE, call_key),
                        target=(NodeKind.SYMBOL, symbol_key),
                        confidence=Confidence.CONFIRMED,
                        evidence=_source_evidence(file.path, call.line, call.line),
                    )
                )
            break


def _extract_webhooks(
    file: IndexedFile,
    candidates: list[ProviderCandidate],
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
) -> None:
    """Link webhook handlers to the provider whose events they handle.

    The event name comes from the handler's own string literals, so a renamed
    event is traceable to the exact line that expects the old name.
    """
    for handler in sorted(file.webhook_symbols):
        symbol_key = f"{file.path}::{handler}"
        symbol = next((s for s in file.symbols if s.qualified_name == handler), None)
        line_start = symbol.line_start if symbol else None
        line_end = symbol.line_end if symbol else None

        # Event names come from any string literal inside the handler, not only
        # call arguments — `event["type"] == "payment.paid"` puts the name in a
        # comparison.
        events = sorted(
            {
                literal.value
                for literal in file.string_literals
                if symbol
                and symbol.line_start <= literal.line <= symbol.line_end
                and _looks_like_event_name(literal.value)
            }
        )

        # A webhook handler often names its provider in the route path or the
        # event strings rather than in an import — `@router.post(
        # "/webhooks/acmepay")` handling `payment.paid` may import only the web
        # framework. Matching on imports alone missed every such handler.
        haystack = " ".join(
            [
                file.path.lower(),
                " ".join(imp.lower() for imp in file.imports),
                (symbol.route_path or "").lower() if symbol else "",
                " ".join(event.lower() for event in events),
                handler.lower(),
            ]
        )

        for candidate in candidates:
            root = candidate.package.split("-")[0].lower()
            if root not in haystack:
                continue

            edges.append(
                EdgeSpec(
                    kind=EdgeKind.HANDLES_WEBHOOK_EVENT,
                    source=(NodeKind.SYMBOL, symbol_key),
                    target=(NodeKind.PROVIDER, candidate.provider_id),
                    confidence=Confidence.CONFIRMED,
                    evidence=_source_evidence(file.path, line_start, line_end),
                    attributes={"events": events},
                )
            )
            break


def _looks_like_event_name(value: str) -> bool:
    """Whether a string literal looks like a provider event name.

    `payment.paid` yes; `/webhooks/acmepay`, `X-Hub-Signature` and prose no.
    Narrow on purpose — a wrong event name is worse than a missing one, because
    it would make the graph claim a handler responds to something it does not.
    """
    if not value or " " in value or "/" in value or value.startswith("_"):
        return False
    if value.count(".") != 1:
        return False
    left, right = value.split(".")
    return bool(left and right and left.islower() and right.islower())


def _extract_permissions(
    index: RepositoryIndex,
    candidates: list[ProviderCandidate],
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
) -> None:
    """Record scope literals found in source.

    **Detected, never requested.** Continuity reads the scopes a project already
    uses so it can notice when a migration would widen them
    (`03_SECURITY_ACCESS.md` §4); it never asks for one.
    """
    seen: set[tuple[str, str]] = set()

    for file in index.files.values():
        for call in file.call_sites:
            for argument in call.arguments_preview:
                if not _SCOPE_PATTERN.match(argument):
                    continue
                for candidate in candidates:
                    root = candidate.package.split("-")[0].lower()
                    if root not in " ".join(file.imports).lower():
                        continue
                    identity = (candidate.provider_id, argument)
                    if identity in seen:
                        continue
                    seen.add(identity)

                    nodes.append(
                        NodeSpec(
                            kind=NodeKind.PERMISSION,
                            key=f"{candidate.provider_id}:{argument}",
                            label=argument,
                            confidence=Confidence.CONFIRMED,
                            evidence=_source_evidence(file.path, call.line, call.line),
                            attributes={"scope": argument, "provider": candidate.provider_id},
                        )
                    )
                    edges.append(
                        EdgeSpec(
                            kind=EdgeKind.REQUIRES_PERMISSION,
                            source=(NodeKind.PROVIDER, candidate.provider_id),
                            target=(NodeKind.PERMISSION, f"{candidate.provider_id}:{argument}"),
                            confidence=Confidence.CONFIRMED,
                            evidence=_source_evidence(file.path, call.line, call.line),
                        )
                    )
                    break


def _extract_test_coverage(
    index: RepositoryIndex, nodes: list[NodeSpec], edges: list[EdgeSpec]
) -> None:
    """Link tests to the symbols they import and exercise.

    Import-based rather than coverage-based: it is deterministic, needs no test
    run, and is right often enough to select which tests a migration must run.
    A real coverage database would be better and is a later refinement.
    """
    symbol_owners: dict[str, list[tuple[str, str]]] = {}
    for file in index.files.values():
        if file.kind is FileKind.TEST:
            continue
        for symbol in file.symbols:
            symbol_owners.setdefault(symbol.qualified_name, []).append(
                (file.path, f"{file.path}::{symbol.qualified_name}")
            )

    for file in index.files.values():
        if file.kind is not FileKind.TEST:
            continue

        nodes.append(
            NodeSpec(
                kind=NodeKind.TEST,
                key=file.path,
                label=file.path,
                confidence=Confidence.CONFIRMED,
                evidence=_source_evidence(file.path),
                attributes={"selector": file.path},
            )
        )

        referenced: set[str] = set()
        for call in file.call_sites:
            referenced.add(call.callee.rsplit(".", 1)[-1])

        for name in sorted(referenced):
            for _owner_path, symbol_key in symbol_owners.get(name, []):
                edges.append(
                    EdgeSpec(
                        kind=EdgeKind.COVERED_BY_TEST,
                        source=(NodeKind.SYMBOL, symbol_key),
                        target=(NodeKind.TEST, file.path),
                        confidence=Confidence.CONFIRMED,
                        evidence=_source_evidence(file.path),
                    )
                )
