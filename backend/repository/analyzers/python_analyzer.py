"""Python source analysis using the standard library `ast` module.

Everything this module produces is `CONFIRMED` evidence: it is derived from the
parse tree, not proposed by a model. That distinction is why the Integration
Mapper agent can be trusted to add only interpretation on top — the facts
underneath it are checkable.

The analysis is deliberately shallow-but-exact. It records what is imported,
what is defined and where, and what is called — not what any of it *means*.
Meaning is the model's job, and conflating the two is how a static analyzer
starts guessing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# Modules whose presence indicates the file talks to something over HTTP.
HTTP_CLIENT_MODULES = frozenset(
    {"requests", "httpx", "aiohttp", "urllib", "urllib3", "http", "grpc"}
)

# Decorators that mark a web route. Matched on the attribute name, so
# `@app.post(...)`, `@router.get(...)` and `@bp.route(...)` all count.
ROUTE_DECORATOR_NAMES = frozenset(
    {"route", "get", "post", "put", "patch", "delete", "head", "options", "websocket"}
)

# Webhook detection needs three signals together, because any one of them alone
# is common in ordinary code. An early version matched on `ast.dump()`
# substrings and flagged five handlers in Continuity's own backend — including
# the detector itself — because "signature" appears in `BadSignature` and "type"
# appears inside `account_type`. Names are now compared whole, and all three
# conditions must hold.
#
# 1. Verification: the handler checks an incoming signature.
WEBHOOK_VERIFY_NAMES = frozenset(
    {
        "verify_signature", "check_signature", "construct_event",
        "constructwebhookevent", "compare_digest", "hmac", "new_hmac",
        "webhook_secret", "verify_webhook", "verify_header",
    }
)
# 2. Dispatch: it branches on an event type.
WEBHOOK_EVENT_NAMES = frozenset(
    {"event", "event_type", "eventtype", "payload", "delivery", "webhook_event"}
)
# 3. Intent: it is a route, or it is named like a hook.
WEBHOOK_NAME_HINTS = ("webhook", "hook", "callback", "notification")


@dataclass(frozen=True, slots=True)
class ImportRecord:
    module: str
    name: str | None
    alias: str | None
    line: int


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """A definition, with the line span retrieval will later slice."""

    qualified_name: str
    kind: str  # "function" | "async_function" | "class" | "method"
    line_start: int
    line_end: int
    decorators: tuple[str, ...] = ()
    is_route: bool = False
    http_method: str | None = None
    route_path: str | None = None


@dataclass(frozen=True, slots=True)
class CallRecord:
    """A call site: what was called, from where, inside which symbol."""

    callee: str
    line: int
    enclosing_symbol: str | None
    arguments_preview: tuple[str, ...] = ()


@dataclass(slots=True)
class PythonAnalysis:
    imports: list[ImportRecord] = field(default_factory=list)
    symbols: list[SymbolRecord] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    http_client_modules: set[str] = field(default_factory=set)
    webhook_symbols: set[str] = field(default_factory=set)
    parse_error: str | None = None


class _Visitor(ast.NodeVisitor):
    def __init__(self, analysis: PythonAnalysis) -> None:
        self.analysis = analysis
        self._scope: list[str] = []

    # --- imports ---

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            self.analysis.imports.append(
                ImportRecord(module=alias.name, name=None, alias=alias.asname, line=node.lineno)
            )
            if root in HTTP_CLIENT_MODULES:
                self.analysis.http_client_modules.add(root)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".")[0]
        for alias in node.names:
            self.analysis.imports.append(
                ImportRecord(
                    module=module, name=alias.name, alias=alias.asname, line=node.lineno
                )
            )
        if root in HTTP_CLIENT_MODULES:
            self.analysis.http_client_modules.add(root)
        self.generic_visit(node)

    # --- definitions ---

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record_symbol(node, "class")
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, "async_function")

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str
    ) -> None:
        actual = "method" if self._scope else kind
        self._record_symbol(node, actual)

        qualified = ".".join([*self._scope, node.name])
        if self._looks_like_webhook(node):
            self.analysis.webhook_symbols.add(qualified)

        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _record_symbol(self, node: ast.AST, kind: str) -> None:
        name = getattr(node, "name", "<anonymous>")
        qualified = ".".join([*self._scope, name])
        decorators = tuple(
            _decorator_name(d) for d in getattr(node, "decorator_list", []) if d is not None
        )
        method, path = _route_from_decorators(getattr(node, "decorator_list", []))

        self.analysis.symbols.append(
            SymbolRecord(
                qualified_name=qualified,
                kind=kind,
                line_start=node.lineno,  # type: ignore[attr-defined]
                line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,  # type: ignore[attr-defined]
                decorators=decorators,
                is_route=method is not None,
                http_method=method,
                route_path=path,
            )
        )

    # --- calls ---

    def visit_Call(self, node: ast.Call) -> None:
        callee = _callee_name(node.func)
        if callee:
            self.analysis.calls.append(
                CallRecord(
                    callee=callee,
                    line=node.lineno,
                    enclosing_symbol=".".join(self._scope) or None,
                    arguments_preview=_argument_preview(node),
                )
            )
        self.generic_visit(node)

    # --- heuristics ---

    def _looks_like_webhook(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """Whether this function handles an inbound provider webhook.

        Requires all three signals: signature verification, event dispatch, and
        either a route decorator or a hook-like name. Two of the three is not
        enough — auth middleware verifies signatures and branches on a payload
        without being a webhook.

        Names are matched whole against the identifiers actually present in the
        subtree, never as substrings of a dumped tree.
        """
        names = _collected_names(node)

        verifies = bool(names & WEBHOOK_VERIFY_NAMES)
        dispatches = bool(names & WEBHOOK_EVENT_NAMES)
        if not (verifies and dispatches):
            return False

        lowered = node.name.lower()
        named_like_hook = any(hint in lowered for hint in WEBHOOK_NAME_HINTS)
        method, _ = _route_from_decorators(node.decorator_list)
        return named_like_hook or method is not None


def _collected_names(node: ast.AST) -> set[str]:
    """Every identifier-like name in a subtree, lowercased and whole.

    Covers variable and attribute names, keyword arguments, parameter names, and
    short string constants (header names such as "X-Hub-Signature" arrive as
    strings, not identifiers).
    """
    names: set[str] = set()
    for child in ast.walk(node):
        match child:
            case ast.Name():
                names.add(child.id.lower())
            case ast.Attribute():
                names.add(child.attr.lower())
            case ast.keyword() if child.arg:
                names.add(child.arg.lower())
            case ast.arg():
                names.add(child.arg.lower())
            case ast.Constant() if isinstance(child.value, str) and len(child.value) <= 64:
                names.add(child.value.lower())
                names.add(child.value.lower().replace("-", "_"))
    return names


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _callee_name(node.func) or "<call>"
    return _callee_name(node) or "<decorator>"


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _route_from_decorators(
    decorators: list[ast.expr],
) -> tuple[str | None, str | None]:
    """Extract the HTTP method and path from a route decorator, if any."""
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        name = _callee_name(decorator.func)
        if not name:
            continue
        attribute = name.rsplit(".", 1)[-1].lower()
        if attribute not in ROUTE_DECORATOR_NAMES:
            continue

        path = None
        if decorator.args and isinstance(decorator.args[0], ast.Constant):
            value = decorator.args[0].value
            if isinstance(value, str):
                path = value

        method = attribute.upper()
        if attribute == "route":
            method = "ANY"
            for keyword in decorator.keywords:
                if keyword.arg == "methods" and isinstance(keyword.value, ast.List):
                    methods = [
                        element.value
                        for element in keyword.value.elts
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                    ]
                    if methods:
                        method = ",".join(methods)
        return method, path
    return None, None


def _argument_preview(node: ast.Call) -> tuple[str, ...]:
    """String and keyword names passed to a call.

    Enough to tell `client.post("/v1/charges")` from `client.post("/v2/pay")`
    without keeping the whole expression.
    """
    preview: list[str] = []
    for argument in node.args[:3]:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            preview.append(argument.value[:120])
    for keyword in node.keywords[:5]:
        if keyword.arg:
            preview.append(f"{keyword.arg}=")
    return tuple(preview)


def analyze_python(source: str) -> PythonAnalysis:
    """Parse and analyze one Python file.

    A syntax error is recorded, not raised: one unparseable file in a target
    repository must not fail the whole scan.
    """
    analysis = PythonAnalysis()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        analysis.parse_error = f"line {exc.lineno}: {exc.msg}"
        return analysis

    _Visitor(analysis).visit(tree)
    return analysis
