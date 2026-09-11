"""Secret redaction for text leaving the process.

This module owns the *content* patterns from `03_SECURITY_ACCESS.md` §2. It is
deliberately small and dependency-free so it can be applied at every enforcement
point without import cycles — logging (C1-04), tool arguments and results,
activity events, retrieved context, and generated diffs.

Path-based exclusion is a separate concern and lands with the repository
indexer (C3-01); this module handles text that is already in hand.

The patterns are intentionally conservative. A false positive redacts a
harmless string, which costs a little debuggability. A false negative leaks a
credential. When the two trade off, redact.
"""

from __future__ import annotations

import re
from typing import Final

REDACTED: Final = "[REDACTED]"

# Each pattern must define exactly the span to be replaced. Where a pattern
# needs surrounding context to match reliably (an assignment, for example), the
# secret itself is captured in group "secret" and only that group is replaced.
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # AWS access key ids have a fixed, unambiguous shape.
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    # GitHub tokens carry a type prefix and a checksummed body.
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    # Google API keys: the classic `AIza…` form and the newer `AQ.` form used
    # by Gemini API keys.
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("gemini_api_key", re.compile(r"\bAQ\.[A-Za-z0-9_-]{20,}\b")),
    # Google OAuth client secrets.
    ("google_oauth_secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b")),
    # Slack and Stripe live keys.
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_live_key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{16,}\b")),
    # JWTs: three base64url segments. Requires a plausible header to avoid
    # matching ordinary dotted identifiers.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # Private key blocks, including the body, across lines.
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Connection strings carrying inline credentials: scheme://user:secret@host
    (
        "connection_string_password",
        re.compile(r"(?P<prefix>://[^\s:/@]+:)(?P<secret>[^\s@/]+)(?P<suffix>@)"),
    ),
    # Generic assignments: api_key = "...", "secret": "...", TOKEN=...
    # Only values long enough to plausibly be a credential are redacted, so
    # `debug=true` and `token=""` survive.
    (
        "generic_assignment",
        re.compile(
            r"(?P<prefix>\b(?:api[_-]?key|secret|password|passwd|token|access[_-]?key|"
            r"private[_-]?key|client[_-]?secret|auth)\b\s*[:=]\s*['\"]?)"
            r"(?P<secret>[A-Za-z0-9_\-./+=]{12,})",
            re.IGNORECASE,
        ),
    ),
)


def redact(text: str) -> str:
    """Return `text` with every recognised secret replaced by `[REDACTED]`.

    Safe to call on arbitrary input, including very large strings and text that
    contains no secrets at all (in which case the original object is returned).
    """
    if not text:
        return text

    result = text
    for _name, pattern in _PATTERNS:
        if "secret" in pattern.groupindex:
            result = pattern.sub(_replace_named_group, result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


def _replace_named_group(match: re.Match[str]) -> str:
    """Replace only the `secret` group, preserving surrounding context.

    Keeping the prefix and suffix means a redacted log line still shows *which*
    setting was involved, which is exactly the debuggability we want without the
    value.
    """
    groups = match.groupdict()
    prefix = groups.get("prefix") or ""
    suffix = groups.get("suffix") or ""
    return f"{prefix}{REDACTED}{suffix}"


def contains_secret(text: str) -> bool:
    """Whether `text` matches any known secret pattern.

    Used where detection matters more than substitution — a secret found inside
    a generated migration patch is a blocking security finding, not something to
    quietly redact (`03_SECURITY_ACCESS.md` §2).
    """
    return any(pattern.search(text) for _name, pattern in _PATTERNS)


def detected_secret_kinds(text: str) -> list[str]:
    """Names of every secret pattern matching `text`, for security findings."""
    return [name for name, pattern in _PATTERNS if pattern.search(text)]


def redact_deep(value: object, _depth: int = 0) -> object:
    """Redact a structured value, however it is nested.

    Lives here rather than in `observability/logging.py`, where it started: it
    is a redaction concern, and the approval API needs it too — a requested
    action is assembled from an agent's proposal and may quote an argument.

    Redacting only top-level strings is not enough: the natural way to log agent
    context is `extra={"tool_args": {...}}`, and a credential inside that dict
    would pass straight through. Anything that is not a plain container is
    stringified and redacted, because `JsonFormatter` will serialise it with
    `default=str` anyway — so an object whose `__repr__` embeds a token would
    otherwise leak.

    `_depth` guards against a pathologically nested or self-referential
    structure; past the limit the value is rendered as a redacted string rather
    than recursed into.
    """
    if _depth > 6:
        return redact(str(value))

    match value:
        case str():
            return redact(value)
        case bool() | int() | float() | None:
            # Numbers cannot carry a secret pattern; leave them typed so the
            # JSON output stays useful for querying.
            return value
        case dict():
            return {k: redact_deep(v, _depth + 1) for k, v in value.items()}
        case list():
            return [redact_deep(item, _depth + 1) for item in value]
        case tuple():
            return [redact_deep(item, _depth + 1) for item in value]
        case set() | frozenset():
            return [redact_deep(item, _depth + 1) for item in value]
        case _:
            return redact(str(value))
