"""TypeScript and JavaScript analysis.

**Deliberately regex-based, and honest about it.** A real parse would need the
TypeScript compiler, which means a Node subprocess for every file — cost and a
second toolchain in the hot path, for a marginal gain at this stage.

What this extracts is the part regexes get right: import specifiers, exported
symbol names, and HTTP-call sites. What it does not attempt is scope resolution,
type inference, or anything requiring a symbol table. Line spans are start-only
for that reason, and the indexer marks these results as lower-fidelity than the
Python AST results so nothing downstream treats them as equivalent.

Upgrading this to a real parser is a known future change, recorded here rather
than in a comment nobody reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_IMPORT = re.compile(
    r"""^\s*import\s+(?:[\w*\s{},]+\s+from\s+)?['"](?P<module>[^'"]+)['"]""",
    re.MULTILINE,
)
_REQUIRE = re.compile(r"""require\(\s*['"](?P<module>[^'"]+)['"]\s*\)""")
_DYNAMIC_IMPORT = re.compile(r"""\bimport\(\s*['"](?P<module>[^'"]+)['"]\s*\)""")

_EXPORTED_FUNCTION = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+(?P<name>\w+)", re.MULTILINE
)
_EXPORTED_CONST = re.compile(
    r"^\s*export\s+(?:default\s+)?const\s+(?P<name>\w+)", re.MULTILINE
)
_EXPORTED_CLASS = re.compile(r"^\s*export\s+(?:default\s+)?class\s+(?P<name>\w+)", re.MULTILINE)

_FETCH_CALL = re.compile(r"""\b(?P<fn>fetch|axios(?:\.\w+)?)\s*\(\s*['"`](?P<url>[^'"`]*)""")

HTTP_CLIENT_PACKAGES = frozenset({"axios", "node-fetch", "got", "superagent", "ky", "undici"})


@dataclass(frozen=True, slots=True)
class TsImport:
    module: str
    line: int


@dataclass(frozen=True, slots=True)
class TsSymbol:
    name: str
    kind: str
    line_start: int


@dataclass(frozen=True, slots=True)
class TsCall:
    callee: str
    target: str
    line: int


@dataclass(slots=True)
class TypeScriptAnalysis:
    imports: list[TsImport] = field(default_factory=list)
    symbols: list[TsSymbol] = field(default_factory=list)
    calls: list[TsCall] = field(default_factory=list)
    http_client_packages: set[str] = field(default_factory=set)
    #: Always True. Recorded on every result so consumers cannot forget that
    #: these facts are weaker than the Python AST's.
    heuristic: bool = True


def _line_of(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


def analyze_typescript(source: str) -> TypeScriptAnalysis:
    analysis = TypeScriptAnalysis()

    for pattern in (_IMPORT, _REQUIRE, _DYNAMIC_IMPORT):
        for match in pattern.finditer(source):
            module = match.group("module")
            analysis.imports.append(TsImport(module=module, line=_line_of(source, match.start())))
            root = module.split("/")[0]
            if root in HTTP_CLIENT_PACKAGES:
                analysis.http_client_packages.add(root)

    for pattern, kind in (
        (_EXPORTED_FUNCTION, "function"),
        (_EXPORTED_CONST, "const"),
        (_EXPORTED_CLASS, "class"),
    ):
        for match in pattern.finditer(source):
            analysis.symbols.append(
                TsSymbol(
                    name=match.group("name"),
                    kind=kind,
                    line_start=_line_of(source, match.start()),
                )
            )

    for match in _FETCH_CALL.finditer(source):
        analysis.calls.append(
            TsCall(
                callee=match.group("fn"),
                target=match.group("url")[:200],
                line=_line_of(source, match.start()),
            )
        )
        if match.group("fn").startswith("axios"):
            analysis.http_client_packages.add("axios")

    return analysis
