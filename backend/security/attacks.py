"""C9-01: deterministic attacks on the code a migration is about to ship.

`backend/security/categories.py` reads the **diff**: what did this patch change
that is dangerous? This module reads the **result**: given the integration code
exactly as it will exist after merge, what does a hostile provider or an
attacker do to it?

The distinction matters, and it is the reason this is not a second copy of the
category detectors. A migration that leaves a payment call with no idempotency
key adds nothing to the diff — nothing was changed, so nothing is flagged — and
still ships a double-charge. A migration that rewrites a client and never sets a
timeout has removed no timeout; there was never one. Diff review cannot see
either. Attacking the post-migration source can.

Every probe here is deterministic, quotes the line it fired on, and produces
`Confidence.CONFIRMED` evidence. The agent in `backend/agents/red_team.py` reads
the same files for what patterns miss, and its findings are `INFERRED` and kept
separate. Neither of them decides anything: `backend/security/policy.py` does.

Each probe fires at most once per file. The output is a list of weaknesses, not
a score, and a file with no weakness produces nothing rather than an assurance.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from typing import Final

from backend.models.enums import AttackClass, Confidence, EvidenceKind, Severity
from backend.models.schemas import Evidence

#: How severe each attack class is when it lands. Only the classes in
#: `BLOCKING_SEVERITIES` stop a run, so this table is the difference between a
#: note on the pull request and a return to the repair loop. It is deliberately
#: conservative: a weakness that costs money or breaks a security control is
#: HIGH or CRITICAL, and a robustness gap is not.
SEVERITY: Final[dict[AttackClass, Severity]] = {
    AttackClass.MALFORMED_RESPONSE: Severity.MEDIUM,
    AttackClass.MISSING_FIELD: Severity.MEDIUM,
    AttackClass.UNEXPECTED_FIELD: Severity.LOW,
    AttackClass.UNEXPECTED_NULL: Severity.LOW,
    AttackClass.EXPIRED_CREDENTIAL: Severity.MEDIUM,
    AttackClass.INVALID_TOKEN: Severity.MEDIUM,
    AttackClass.WEBHOOK_REPLAY: Severity.HIGH,
    AttackClass.WEBHOOK_DUPLICATION: Severity.MEDIUM,
    AttackClass.DUPLICATE_TRANSACTION: Severity.HIGH,
    AttackClass.TIMEOUT: Severity.MEDIUM,
    AttackClass.RETRY_STORM: Severity.HIGH,
    AttackClass.RATE_LIMIT: Severity.LOW,
    AttackClass.MALICIOUS_EXTERNAL_TEXT: Severity.MEDIUM,
    AttackClass.PROMPT_INJECTION: Severity.HIGH,
    AttackClass.UNAUTHORIZED_TOOL: Severity.CRITICAL,
    AttackClass.PERMISSION_ESCALATION: Severity.HIGH,
    AttackClass.INVALID_SIGNATURE: Severity.CRITICAL,
}

#: A finding at or above this severity returns the run to the repair loop.
BLOCKING_SEVERITIES: Final = frozenset({Severity.HIGH, Severity.CRITICAL})


@dataclass(frozen=True, slots=True)
class Weakness:
    """One attack that would land, and the line it lands on."""

    attack: AttackClass
    severity: Severity
    summary: str
    excerpt: str
    path: str
    line: int | None = None

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES

    def evidence(self) -> Evidence:
        return Evidence(
            kind=EvidenceKind.SOURCE,
            confidence=Confidence.CONFIRMED,
            file_path=self.path,
            line_start=self.line,
            line_end=self.line,
            excerpt=self.excerpt[:1000],
        )

    def summary_dict(self) -> dict[str, object]:
        return {
            "attack": self.attack.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "path": self.path,
            "line": self.line,
        }


# --- Vocabulary -----------------------------------------------------------

#: Method names that send a request. Matched on the attribute, so this covers
#: `requests.get`, `httpx.AsyncClient().post`, and `self._session.put` alike
#: without needing to know which library the project chose.
_HTTP_METHODS: Final = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "send"}
)

#: Receivers that make an attribute call an HTTP call rather than, say, a dict
#: lookup. `data.get("x")` must not be read as a network request.
_HTTP_RECEIVERS: Final = re.compile(
    r"(requests|httpx|session|client|http|api|transport|urllib|aiohttp|_?conn)",
    re.IGNORECASE,
)

_STATUS_CHECK: Final = re.compile(
    r"raise_for_status|status_code|\.status\b|\bresponse\.ok\b|\bresp\.ok\b|is_success",
    re.IGNORECASE,
)
_AUTHENTICATES: Final = re.compile(
    r"bearer|authorization|api[_-]?key|access[_-]?token|basic\s+auth|\bsecret_key\b",
    re.IGNORECASE,
)
_HANDLES_EXPIRY: Final = re.compile(
    r"\b401\b|unauthorized|refresh[_-]?token|token[_-]?expired|reauth|expires_at",
    re.IGNORECASE,
)
_HANDLES_RATE_LIMIT: Final = re.compile(
    r"\b429\b|rate[_ -]?limit|retry[_-]?after|too\s+many\s+requests", re.IGNORECASE
)
_IDEMPOTENT: Final = re.compile(r"idempoten", re.IGNORECASE)
_MUTATING: Final = re.compile(
    r"charge|payment|pay\b|transfer|refund|capture|payout|order|purchase|checkout|invoice|subscri",
    re.IGNORECASE,
)
_WEBHOOK: Final = re.compile(r"webhook|callback_url|event_payload", re.IGNORECASE)
_SIGNATURE: Final = re.compile(
    r"signature|x-[a-z-]*-signature|hmac|digest", re.IGNORECASE
)
_CONSTANT_TIME: Final = re.compile(r"compare_digest|constant_time|secrets\.compare")
_FRESHNESS: Final = re.compile(
    r"timestamp|tolerance|\bage\b|time\.time|utcnow|now\(\)|expires", re.IGNORECASE
)
_DEDUPLICATES: Final = re.compile(
    r"idempoten|already[_ ]processed|seen[_ ]?(id|event)|event_id|delivery_id|unique",
    re.IGNORECASE,
)
_BACKOFF: Final = re.compile(r"sleep|backoff|wait|jitter|delay", re.IGNORECASE)
_TIMEOUT_SET: Final = re.compile(r"timeout\s*=")
_EQUALITY: Final = re.compile(r"[!=]=")
_RETRIES: Final = re.compile(r"retry|retries|attempt", re.IGNORECASE)
_ESCALATES: Final = re.compile(
    r"""scope[s]?\s*[:=]\s*['"][^'"]*(admin|write|\*|full)"""
    r"""|role\s*[:=]\s*['"]?(admin|owner|superuser|root)"""
    r"""|\.admin\b|sudo\b|allow[_-]?all""",
    re.IGNORECASE,
)
#: Functions that execute or deserialise, which migrated integration code has no
#: business calling. `subprocess` and friends are matched on the module too.
_DANGEROUS_CALLS: Final = frozenset(
    {"system", "popen", "eval", "exec", "compile", "loads", "load", "check_output", "run"}
)
_DANGEROUS_MODULES: Final = frozenset({"os", "subprocess", "pickle", "marshal", "shelve"})

#: Sinks that render or execute whatever text they are handed.
_TEXT_SINKS: Final = frozenset(
    {
        "Markup",
        "render_template_string",
        "HTML",
        "innerHTML",
        "execute",
        "executescript",
        "system",
        "write_html",
    }
)
#: Sinks that hand text to a model. Provider prose reaching one of these is the
#: prompt-injection path `03_SECURITY_ACCESS.md` §9 exists to prevent.
_MODEL_SINKS: Final = frozenset(
    {
        "invoke",
        "complete",
        "completion",
        "generate",
        "chat",
        "predict",
        "ask",
        "converse",
        "__call__",
    }
)
_MODEL_KEYWORDS: Final = frozenset({"prompt", "messages", "system_prompt", "input_text"})


def attack(files: dict[str, str]) -> list[Weakness]:
    """Run every probe over the post-migration source of every file.

    `files` maps a repository-relative path to its content *after* the patch was
    applied — not a diff. A file that cannot be parsed is still probed by the
    text-only rules, because a syntax error in one file is not a reason to stop
    attacking the rest.
    """
    weaknesses: list[Weakness] = []
    for path in sorted(files):
        weaknesses.extend(_attack_file(path, files[path]))
    return weaknesses


def _attack_file(path: str, source: str) -> list[Weakness]:
    found: list[Weakness] = []
    lines = source.splitlines()
    # What the probes match on: the same lines with prose blanked out. Findings
    # still quote `lines`, so the excerpt is the code as it was written.
    code = code_lines(source, lines)

    tree: ast.Module | None = None
    if path.endswith(".py"):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None

    if tree is not None:
        found.extend(_probe_timeout(path, tree, lines, code))
        found.extend(_probe_malformed_response(path, tree, lines))
        found.extend(_probe_field_handling(path, tree, lines))
        found.extend(_probe_dangerous_calls(path, tree, lines))
        found.extend(_probe_untrusted_sinks(path, tree, lines))
        found.extend(_probe_retry_storm(path, tree, lines, code))

    found.extend(_probe_credentials(path, lines, code))
    found.extend(_probe_webhook(path, lines, code))
    found.extend(_probe_signature(path, lines, code))
    found.extend(_probe_transactions(path, lines, code, tree))
    found.extend(_probe_rate_limit(path, lines, code, tree))
    found.extend(_probe_escalation(path, lines, code))

    return _first_per_class(found)


def _first_per_class(found: list[Weakness]) -> list[Weakness]:
    """One finding per attack class per file.

    A client with nine unguarded requests has one timeout problem, not nine. The
    engineer fixing it reads the first example and fixes the pattern; the other
    eight are noise that buries the classes that fired once.
    """
    seen: set[AttackClass] = set()
    kept: list[Weakness] = []
    for weakness in found:
        if weakness.attack in seen:
            continue
        seen.add(weakness.attack)
        kept.append(weakness)
    return kept


# --- Helpers --------------------------------------------------------------


def _excerpt(lines: list[str], line: int | None) -> str:
    if line is None or line < 1 or line > len(lines):
        return ""
    return lines[line - 1].strip()


def _make(
    attack_class: AttackClass,
    *,
    path: str,
    line: int | None,
    lines: list[str],
    summary: str,
) -> Weakness:
    return Weakness(
        attack=attack_class,
        severity=SEVERITY[attack_class],
        summary=summary,
        excerpt=_excerpt(lines, line),
        path=path,
        line=line,
    )


def _receiver_text(node: ast.Attribute) -> str:
    try:
        return ast.unparse(node.value)
    except Exception:  # pragma: no cover - unparse covers every real AST node
        return ""


def _is_http_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in _HTTP_METHODS:
        return False
    return bool(_HTTP_RECEIVERS.search(_receiver_text(func)))


def _http_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_http_call(node)
    ]


def _keywords(node: ast.Call) -> set[str]:
    return {kw.arg for kw in node.keywords if kw.arg is not None}


def _json_variables(tree: ast.Module) -> dict[str, int]:
    """Names assigned from a `.json()` call: the provider's own words.

    Tracking the assignment rather than the call is what lets the field probes
    fire on `data["currency"]` three lines later. It is deliberately shallow —
    one hop, same module — because a longer chain is exactly where a static
    guess becomes a false accusation.
    """
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "json"):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = node.lineno
    return found


def _enclosing_functions(tree: ast.Module) -> dict[int, ast.AST]:
    """Map every node id to the function that contains it."""
    owner: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for child in ast.walk(node):
            owner.setdefault(id(child), node)
    return owner


def _guarded_by_try(tree: ast.Module, target: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for child in ast.walk(node):
            if child is target:
                return True
    return False


# --- Probes ---------------------------------------------------------------


def _probe_timeout(
    path: str, tree: ast.Module, lines: list[str], code: list[str]
) -> list[Weakness]:
    """A request with no deadline hangs the workflow when the provider does."""
    # A client constructed with a timeout applies it to every call it makes, so
    # per-call keywords are not the only place a deadline can live.
    if _in_code(code, _TIMEOUT_SET):
        return []
    for call in _http_calls(tree):
        if "timeout" not in _keywords(call):
            return [
                _make(
                    AttackClass.TIMEOUT,
                    path=path,
                    line=call.lineno,
                    lines=lines,
                    summary=(
                        "A provider request is made with no timeout. A provider "
                        "that accepts the connection and never answers holds this "
                        "call open indefinitely."
                    ),
                )
            ]
    return []


def _probe_malformed_response(
    path: str, tree: ast.Module, lines: list[str]
) -> list[Weakness]:
    """`.json()` on an HTML error page raises where nothing catches it."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "json"):
            continue
        if _guarded_by_try(tree, node):
            continue
        return [
            _make(
                AttackClass.MALFORMED_RESPONSE,
                path=path,
                line=node.lineno,
                lines=lines,
                summary=(
                    "A provider response is decoded as JSON with no handler for "
                    "the decode failing. A gateway error page or a truncated "
                    "body raises out of this call."
                ),
            )
        ]
    return []


def _probe_field_handling(
    path: str, tree: ast.Module, lines: list[str]
) -> list[Weakness]:
    """What the code assumes about the shape of the provider's answer."""
    provider_vars = _json_variables(tree)
    if not provider_vars:
        return []

    found: list[Weakness] = []
    for node in ast.walk(tree):
        # Unexpected field: the whole payload splatted into a constructor. One
        # new key the provider added and this raises TypeError.
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Name):
                    if kw.value.id in provider_vars:
                        found.append(
                            _make(
                                AttackClass.UNEXPECTED_FIELD,
                                path=path,
                                line=node.lineno,
                                lines=lines,
                                summary=(
                                    "The provider's response is expanded into a "
                                    "call's arguments. Any field the provider "
                                    "adds becomes an unexpected keyword."
                                ),
                            )
                        )

        # Missing field: a required subscript with no default.
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id in provider_vars and isinstance(node.slice, ast.Constant):
                found.append(
                    _make(
                        AttackClass.MISSING_FIELD,
                        path=path,
                        line=node.lineno,
                        lines=lines,
                        summary=(
                            f"`{ast.unparse(node)}` requires the provider to send "
                            "that field. A response without it raises KeyError "
                            "rather than being handled."
                        ),
                    )
                )

        # Unexpected null: a value read with a default of None and then used as
        # if it were a number. `.get()` avoids the KeyError and moves the crash.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"int", "float", "Decimal", "round", "abs"}:
                for arg in node.args:
                    if _reads_provider_value(arg, provider_vars):
                        found.append(
                            _make(
                                AttackClass.UNEXPECTED_NULL,
                                path=path,
                                line=node.lineno,
                                lines=lines,
                                summary=(
                                    "A provider value is converted to a number "
                                    "with no null check. A field the provider "
                                    "sends as null fails here."
                                ),
                            )
                        )
    return found


def _reads_provider_value(node: ast.AST, provider_vars: dict[str, int]) -> bool:
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return node.value.id in provider_vars
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and isinstance(node.func.value, ast.Name):
            return node.func.value.id in provider_vars
    return False


def _probe_dangerous_calls(
    path: str, tree: ast.Module, lines: list[str]
) -> list[Weakness]:
    """Integration code that executes or deserialises is doing someone's work.

    A migration is supposed to change how the project talks to a provider. A
    patch that reaches for `subprocess` or `pickle.loads` on the way is either
    confused or being steered, and neither is something to deliver.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Name) and node.func.id in {
            "eval",
            "exec",
            "compile",
            "__import__",
        }:
            name = node.func.id
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _DANGEROUS_CALLS:
            receiver = _receiver_text(node.func).split(".")[0]
            if receiver in _DANGEROUS_MODULES:
                name = f"{receiver}.{node.func.attr}"
        if name:
            return [
                _make(
                    AttackClass.UNAUTHORIZED_TOOL,
                    path=path,
                    line=node.lineno,
                    lines=lines,
                    summary=(
                        f"`{name}` appears in migrated integration code. Changing "
                        "how a provider is called never requires executing or "
                        "deserialising arbitrary input."
                    ),
                )
            ]
    return []


def _probe_untrusted_sinks(
    path: str, tree: ast.Module, lines: list[str]
) -> list[Weakness]:
    """Where the provider's own text ends up.

    `03_SECURITY_ACCESS.md` §9 treats provider content as untrusted input. Code
    that pipes a response field into a template, a query, or a model prompt has
    handed an outside party the keyboard.
    """
    provider_vars = _json_variables(tree)
    if not provider_vars:
        return []

    found: list[Weakness] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        keyword_names = _keywords(node)
        carries_provider_text = any(
            _mentions_provider_value(arg, provider_vars) for arg in arguments
        )
        if not carries_provider_text:
            continue

        if name in _TEXT_SINKS:
            found.append(
                _make(
                    AttackClass.MALICIOUS_EXTERNAL_TEXT,
                    path=path,
                    line=node.lineno,
                    lines=lines,
                    summary=(
                        f"Provider-supplied text reaches `{name}`, which renders "
                        "or executes what it is given. The provider controls that "
                        "string."
                    ),
                )
            )
        elif name in _MODEL_SINKS or keyword_names & _MODEL_KEYWORDS:
            found.append(
                _make(
                    AttackClass.PROMPT_INJECTION,
                    path=path,
                    line=node.lineno,
                    lines=lines,
                    summary=(
                        "Provider-supplied text is passed to a model call. "
                        "Instructions in a provider's response would be read as "
                        "instructions."
                    ),
                )
            )
    return found


def _mentions_provider_value(node: ast.AST, provider_vars: dict[str, int]) -> bool:
    """Whether this expression carries a provider value anywhere inside it."""
    for child in ast.walk(node):
        if _reads_provider_value(child, provider_vars):
            return True
        if isinstance(child, ast.Name) and child.id in provider_vars:
            return True
    return False


def _probe_retry_storm(
    path: str, tree: ast.Module, lines: list[str], code: list[str]
) -> list[Weakness]:
    """A retry with no cap or no pause turns an outage into an attack."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.While):
            continue
        if not (isinstance(node.test, ast.Constant) and node.test.value is True):
            continue
        if any(_is_http_call(child) for child in ast.walk(node) if isinstance(child, ast.Call)):
            if not any(isinstance(child, ast.Break) for child in ast.walk(node)):
                return [
                    _make(
                        AttackClass.RETRY_STORM,
                        path=path,
                        line=node.lineno,
                        lines=lines,
                        summary=(
                            "A provider request is retried in an unbounded loop "
                            "with no exit. A provider returning errors is hammered "
                            "until something else stops it."
                        ),
                    )
                ]

    retry_line = _anchor_line(code, _RETRIES)
    if retry_line is not None and _http_calls(tree) and not _in_code(code, _BACKOFF):
        line = retry_line
        return [
            _make(
                AttackClass.RETRY_STORM,
                path=path,
                line=line,
                lines=lines,
                summary=(
                    "Requests are retried with no pause between attempts. A "
                    "provider that is already failing is retried as fast as the "
                    "process can issue calls."
                ),
            )
        ]
    return []


_COMMENT_PREFIXES: Final = ("#", "//")
_FENCES: Final = ('"""', "'''")


def code_lines(source: str, lines: list[str]) -> list[str]:
    """`lines` with comments and docstrings blanked out, character for character.

    Everything a probe *matches on* reads this; everything a probe *quotes*
    reads the original line, so a finding still shows the code as written.

    Blanking rather than dropping keeps line numbers and columns aligned, and it
    is why code sharing a line with a docstring close is still seen — an earlier
    version skipped the whole line and silently disabled the probe.

    Ordinary string literals are left alone. `SCOPES = "charges.admin"` is code
    and must match; only a string that is a statement by itself is prose.
    """
    try:
        return _blank_python(source, lines)
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        # Not Python, or not parseable. Fall back to the cheap rule: drop from
        # the first comment marker on. It cannot see docstrings, which is worth
        # stating rather than pretending otherwise.
        return [_strip_comment(text) for text in lines]


def _strip_comment(text: str) -> str:
    for marker in _COMMENT_PREFIXES:
        index = text.find(marker)
        if index != -1:
            text = text[:index]
    return text


def _blank_python(source: str, lines: list[str]) -> list[str]:
    """Blank every comment and standalone string expression."""
    blanked = list(lines)
    readline = io.StringIO(source).readline

    previous = tokenize.NEWLINE
    for token in tokenize.generate_tokens(readline):
        prose = token.type == tokenize.COMMENT or (
            token.type == tokenize.STRING
            # A string is a docstring when it stands alone as a statement: the
            # token before it ends a statement or opens a block.
            and previous
            in {
                tokenize.NEWLINE,
                tokenize.NL,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.ENCODING,
            }
        )
        if prose:
            _blank_span(blanked, token)
        if token.type not in {tokenize.COMMENT, tokenize.NL}:
            previous = token.type
    return blanked


def _blank_span(blanked: list[str], token: tokenize.TokenInfo) -> None:
    first, start_col = token.start
    last, end_col = token.end
    for number in range(first, last + 1):
        if number > len(blanked):
            break
        text = blanked[number - 1]
        begin = start_col if number == first else 0
        finish = end_col if number == last else len(text)
        blanked[number - 1] = text[:begin] + " " * (finish - begin) + text[finish:]


def _anchor_line(code: list[str], anchor: re.Pattern[str]) -> int | None:
    """The first line of real code matching `anchor`.

    A probe that quotes the module docstring because it happens to contain the
    word "payment" is technically pointing at a match and practically pointing
    at nothing — the engineer reading the finding needs the line that does the
    thing, not the line that describes it.
    """
    for number, text in enumerate(code, start=1):
        if anchor.search(text):
            return number
    return None


def _text_probe(
    attack_class: AttackClass,
    *,
    path: str,
    lines: list[str],
    code: list[str],
    anchor: re.Pattern[str],
    summary: str,
    line: int | None = None,
) -> list[Weakness]:
    """Fire on the first code line matching `anchor`, quoting it.

    No line, no finding. A CONFIRMED-confidence weakness that cannot quote the
    code it is about is not evidence, and — at a blocking severity — is an
    unanswerable complaint that would return a run to the repair loop with
    nothing an engineer could fix.
    """
    if line is None:
        line = _anchor_line(code, anchor)
    if line is None:
        return []
    return [_make(attack_class, path=path, line=line, lines=lines, summary=summary)]


def _in_code(code: list[str], pattern: re.Pattern[str]) -> bool:
    """Whether `pattern` appears on a line that actually runs.

    Every presence *and* absence check goes through this rather than searching
    the raw text. Both directions were wrong otherwise: a comment mentioning an
    old `scope="full"` raised a blocking escalation finding on code that had
    just narrowed its scope, and a comment mentioning a timeout suppressed a
    real one.
    """
    return _anchor_line(code, pattern) is not None


def _probe_credentials(
    path: str, lines: list[str], code: list[str]
) -> list[Weakness]:
    if not _in_code(code, _AUTHENTICATES):
        return []

    found: list[Weakness] = []
    if not _in_code(code, _HANDLES_EXPIRY):
        found.extend(
            _text_probe(
                AttackClass.EXPIRED_CREDENTIAL,
                path=path,
                lines=lines,
                code=code,
                anchor=_AUTHENTICATES,
                summary=(
                    "The client authenticates but handles no credential expiry. "
                    "When the token expires, every call fails as though the "
                    "provider were down."
                ),
            )
        )
    if not _in_code(code, _STATUS_CHECK):
        found.extend(
            _text_probe(
                AttackClass.INVALID_TOKEN,
                path=path,
                lines=lines,
                code=code,
                anchor=_AUTHENTICATES,
                summary=(
                    "No response status is checked. A rejected credential returns "
                    "an error body that is parsed as though it were data."
                ),
            )
        )
    return found


def _probe_webhook(
    path: str, lines: list[str], code: list[str]
) -> list[Weakness]:
    if not _in_code(code, _WEBHOOK):
        return []

    found: list[Weakness] = []
    if not _in_code(code, _FRESHNESS):
        found.extend(
            _text_probe(
                AttackClass.WEBHOOK_REPLAY,
                path=path,
                lines=lines,
                code=code,
                anchor=_WEBHOOK,
                summary=(
                    "A webhook is accepted with no freshness check. A captured "
                    "delivery replayed later is indistinguishable from a new one."
                ),
            )
        )
    if not _in_code(code, _DEDUPLICATES):
        found.extend(
            _text_probe(
                AttackClass.WEBHOOK_DUPLICATION,
                path=path,
                lines=lines,
                code=code,
                anchor=_WEBHOOK,
                summary=(
                    "A webhook is processed with no delivery-id deduplication. "
                    "Providers retry, so the same event is handled twice."
                ),
            )
        )
    return found


def _probe_signature(
    path: str, lines: list[str], code: list[str]
) -> list[Weakness]:
    if not _in_code(code, _SIGNATURE):
        return []
    if _in_code(code, _CONSTANT_TIME):
        return []
    if not _in_code(code, _EQUALITY):
        # A signature is mentioned and never compared at all. That is the
        # verification-absent case, reported as the same class because the
        # attacker's move — send whatever signature you like — is the same.
        return _text_probe(
            AttackClass.INVALID_SIGNATURE,
            path=path,
            lines=lines,
                code=code,
            anchor=_SIGNATURE,
            summary=(
                "A signature is read and never verified. An unsigned or "
                "wrongly-signed request is accepted."
            ),
        )
    return _text_probe(
        AttackClass.INVALID_SIGNATURE,
        path=path,
        lines=lines,
                code=code,
        anchor=_SIGNATURE,
        summary=(
            "A signature is compared with `==` rather than a constant-time "
            "comparison. The comparison leaks how much of a forged signature "
            "was right."
        ),
    )


#: Request methods that cannot move money on their own.
_READ_ONLY_METHODS: Final = frozenset({"get", "head", "options"})


def _mutating_call_line(tree: ast.Module | None, code: list[str]) -> int | None:
    """The line of the first request that moves money.

    Matched on the request itself — the method it uses and the URL it posts to —
    rather than on the word "payment" appearing somewhere in the file. A module
    docstring that mentions payments is not a charge.
    """
    if tree is None:
        return _anchor_line(code, _MUTATING)
    for call in _http_calls(tree):
        method = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        if method in _READ_ONLY_METHODS:
            continue
        if _MUTATING.search(ast.unparse(call)):
            return call.lineno
    return None


def _probe_transactions(
    path: str, lines: list[str], code: list[str], tree: ast.Module | None
) -> list[Weakness]:
    if _in_code(code, _IDEMPOTENT):
        return []
    line = _mutating_call_line(tree, code)
    if line is None:
        return []
    return _text_probe(
        AttackClass.DUPLICATE_TRANSACTION,
        path=path,
        lines=lines,
                code=code,
        anchor=_MUTATING,
        line=line,
        summary=(
            "A money-moving call carries no idempotency key. A retried or "
            "duplicated request charges twice."
        ),
    )


def _probe_rate_limit(
    path: str, lines: list[str], code: list[str], tree: ast.Module | None
) -> list[Weakness]:
    if _in_code(code, _HANDLES_RATE_LIMIT):
        return []

    if tree is not None:
        calls = _http_calls(tree)
        if not calls:
            return []
        line: int | None = calls[0].lineno
    else:
        line = _anchor_line(code, _HTTP_RECEIVERS)
        if line is None:
            return []

    return _text_probe(
        AttackClass.RATE_LIMIT,
        path=path,
        lines=lines,
                code=code,
        anchor=_HTTP_RECEIVERS,
        line=line,
        summary=(
            "No rate-limit response is handled. A 429 is treated as an ordinary "
            "failure and the Retry-After the provider sent is ignored."
        ),
    )


def _probe_escalation(
    path: str, lines: list[str], code: list[str]
) -> list[Weakness]:
    if not _in_code(code, _ESCALATES):
        return []
    return _text_probe(
        AttackClass.PERMISSION_ESCALATION,
        path=path,
        lines=lines,
                code=code,
        anchor=_ESCALATES,
        summary=(
            "The migrated code asks the provider for administrative or "
            "write-everything access. A migration should not widen what the "
            "project is allowed to do."
        ),
    )
