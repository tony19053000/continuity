"""Deterministic detection for the finding categories code can see.

The Security Reviewer agent (C8-01) reads a diff and reasons about it. Some
categories, though, are not a matter of judgment: a credential in a patch is a
regex match, a new dependency is a manifest comparison, a removed signature
check is a token that used to be there and is not any more. Where code can
answer, code answers — and the agent's opinion is recorded beside it rather than
instead of it.

This module is that half. It runs on every patch regardless of whether a model
is reachable, so the security floor does not depend on a model being available.
Each detector returns evidence quoting the diff line it fired on, because a
finding a reviewer cannot locate is a finding they will ignore.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from backend.models.enums import Confidence, EvidenceKind, FindingCategory, Severity
from backend.models.schemas import Evidence
from backend.shared.redaction import contains_secret, detected_secret_kinds

#: An added line in a unified diff: `+foo` but not the `+++ b/path` header.
_ADDED: Final = re.compile(r"^\+(?!\+\+)(?P<body>.*)$", re.MULTILINE)
_REMOVED: Final = re.compile(r"^-(?!--)(?P<body>.*)$", re.MULTILINE)
_FILE_HEADER: Final = re.compile(r"^\+\+\+ b/(?P<path>.+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class DetectedFinding:
    """One thing code noticed in a patch, with the line it noticed it on."""

    category: FindingCategory
    severity: Severity
    summary: str
    excerpt: str
    path: str | None = None

    def evidence(self) -> Evidence:
        return Evidence(
            kind=EvidenceKind.SOURCE,
            confidence=Confidence.CONFIRMED,
            file_path=self.path,
            excerpt=self.excerpt[:1000],
        )


@dataclass(frozen=True, slots=True)
class DiffLine:
    path: str | None
    text: str


def added_lines(diff: str) -> Iterator[DiffLine]:
    """Every added line, tagged with the file it was added to."""
    path: str | None = None
    for raw in diff.splitlines():
        header = _FILE_HEADER.match(raw)
        if header:
            path = header.group("path")
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            yield DiffLine(path=path, text=raw[1:])


def removed_lines(diff: str) -> Iterator[DiffLine]:
    path: str | None = None
    for raw in diff.splitlines():
        header = _FILE_HEADER.match(raw)
        if header:
            path = header.group("path")
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            yield DiffLine(path=path, text=raw[1:])


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

#: Signature and webhook verification, by the names these things actually have.
_VERIFICATION: Final = re.compile(
    r"verify_signature|check_signature|constant_time|compare_digest|hmac|"
    r"verify_webhook|signature_valid|verify_header",
    re.IGNORECASE,
)

#: Turning verification off rather than removing it.
_VERIFICATION_DISABLED: Final = re.compile(
    r"verify\s*=\s*False|ssl_verify\s*=\s*False|verify_ssl\s*=\s*False|"
    r"check_hostname\s*=\s*False|CERT_NONE|skip_verification\s*=\s*True|"
    r"InsecureRequestWarning",
    re.IGNORECASE,
)

#: Authorization checks, which a migration has no business removing.
_AUTHORIZATION: Final = re.compile(
    r"require_auth|check_permission|has_permission|is_authorized|require_scope|"
    r"@login_required|permission_classes|authorize\(|current_user",
    re.IGNORECASE,
)

_AUTHENTICATION: Final = re.compile(
    r"Authorization[\"']?\s*[:=]|Bearer\s|api[_-]?key|oauth|access_token|"
    r"client_secret|basic_auth|auth\s*=",
    re.IGNORECASE,
)

_SCOPE: Final = re.compile(r"scope[s]?\s*[:=]|\bscope=|\.write\b|\.admin\b", re.IGNORECASE)

#: Privilege words that appear when a role or permission is widened.
_PRIVILEGE: Final = re.compile(
    r"\badmin\b|\bsuperuser\b|\broot\b|\bsudo\b|\*\s*:\s*\*|allow_all|"
    r"grant_all|is_staff\s*=\s*True",
    re.IGNORECASE,
)

#: Retry without a ceiling, or retrying something that should not be retried.
_DANGEROUS_RETRY: Final = re.compile(
    r"while\s+True.*retry|retries\s*=\s*(?:-1|None|float\(|9{3,})|"
    r"max_retries\s*=\s*(?:-1|None|9{3,})|retry_forever|infinite_retry",
    re.IGNORECASE,
)

#: A write repeated without an idempotency key is a duplicate charge waiting.
_MUTATING_CALL: Final = re.compile(
    r"\.post\(|\.put\(|\.patch\(|\.delete\(|method\s*=\s*[\"'](?:POST|PUT|PATCH)",
    re.IGNORECASE,
)
_IDEMPOTENCY: Final = re.compile(r"idempoten", re.IGNORECASE)

#: Parameters that turn an external value into behaviour.
_UNSAFE_PARAMETER: Final = re.compile(
    r"\beval\(|\bexec\(|pickle\.loads|yaml\.load\s*\((?![^)]*Loader)|"
    r"os\.system|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True|"
    r"shell\s*=\s*True|__import__\(",
    re.IGNORECASE,
)

#: Instruction-shaped text arriving through a patch rather than a changelog.
_INJECTION: Final = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"you\s+are\s+now\s+(?:a|an)\b|disregard\s+(?:your|the)\s+(?:rules|instructions)|"
    r"new\s+system\s+prompt|</?(?:system|instruction)>",
    re.IGNORECASE,
)

_TEST_PATH: Final = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|[^/]+_test\.(py|go|ts|js)$"
    r"|[^/]+\.(test|spec)\.(ts|tsx|js|jsx)$",
    re.IGNORECASE,
)

_MANIFESTS: Final = frozenset(
    {"pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml"}
)


def changed_paths(diff: str) -> list[str]:
    """Every file the diff touches, by its post-image path."""
    return sorted({match.group("path") for match in _FILE_HEADER.finditer(diff)})


def detect(diff: str, *, impact_set: list[str] | None = None) -> list[DetectedFinding]:
    """Every category code can decide, found in one pass over the diff.

    `impact_set` is what makes `tool_misuse` detectable here: a file in the diff
    that correlation never said this change reaches is the agent having gone
    somewhere it was not sent. Omitting the impact set simply skips that check
    rather than flagging everything.
    """
    if not diff.strip():
        return []

    added = list(added_lines(diff))
    removed = list(removed_lines(diff))
    findings: list[DetectedFinding] = []

    findings.extend(_tool_misuse(diff, impact_set))
    findings.extend(_secrets(added))
    findings.extend(_verification(added, removed))
    findings.extend(_authorization(added, removed))
    findings.extend(_authentication(added))
    findings.extend(_scopes(added))
    findings.extend(_privilege(added))
    findings.extend(_unsafe_parameters(added))
    findings.extend(_dangerous_retry(added))
    findings.extend(_duplicate_transaction(added))
    findings.extend(_new_dependency(added))
    findings.extend(_injection(added))
    findings.extend(_test_weakened(added, removed))
    return findings


def _tool_misuse(diff: str, impact_set: list[str] | None) -> list[DetectedFinding]:
    """Files changed that correlation never said this change reaches.

    The patch rules (C7-02) already discard an unjustified out-of-scope edit, so
    anything reaching here arrived with a justification or through a command the
    migration ran. Either way a reviewer should see it — a justified scope
    escape is still a scope escape.
    """
    if impact_set is None:
        return []

    allowed = set(impact_set)
    strayed = [path for path in changed_paths(diff) if path not in allowed]
    if not strayed:
        return []

    return [
        DetectedFinding(
            category=FindingCategory.TOOL_MISUSE,
            severity=Severity.MEDIUM,
            summary=(
                "The patch changes files outside the correlated impact set: "
                f"{', '.join(strayed)}."
            ),
            excerpt=f"outside the impact set: {', '.join(strayed)}",
            path=strayed[0],
        )
    ]


def _secrets(added: list[DiffLine]) -> list[DetectedFinding]:
    found = []
    for line in added:
        if contains_secret(line.text):
            kinds = ", ".join(detected_secret_kinds(line.text))
            found.append(
                DetectedFinding(
                    category=FindingCategory.SECRET_EXPOSURE,
                    severity=Severity.CRITICAL,
                    summary=f"The patch adds a credential ({kinds}) to {line.path}.",
                    # The line itself is never quoted — that would put the
                    # credential in the finding, the audit row, and the PR body.
                    excerpt=f"a {kinds} was added to {line.path}",
                    path=line.path,
                )
            )
    return found


def _verification(
    added: list[DiffLine], removed: list[DiffLine]
) -> list[DetectedFinding]:
    found = []

    for line in added:
        if _VERIFICATION_DISABLED.search(line.text):
            found.append(
                DetectedFinding(
                    category=FindingCategory.WEBHOOK_VERIFICATION,
                    severity=Severity.HIGH,
                    summary=f"The patch disables verification in {line.path}.",
                    excerpt=line.text.strip(),
                    path=line.path,
                )
            )

    added_text = "\n".join(line.text for line in added)
    for line in removed:
        if _VERIFICATION.search(line.text) and not _VERIFICATION.search(added_text):
            found.append(
                DetectedFinding(
                    category=FindingCategory.WEBHOOK_VERIFICATION,
                    severity=Severity.CRITICAL,
                    summary=(
                        f"The patch removes a signature or webhook verification "
                        f"check from {line.path} and adds nothing in its place."
                    ),
                    excerpt=line.text.strip(),
                    path=line.path,
                )
            )
            break
    return found


def _authorization(
    added: list[DiffLine], removed: list[DiffLine]
) -> list[DetectedFinding]:
    added_text = "\n".join(line.text for line in added)
    for line in removed:
        if _AUTHORIZATION.search(line.text) and not _AUTHORIZATION.search(added_text):
            return [
                DetectedFinding(
                    category=FindingCategory.AUTHORIZATION_WEAKENED,
                    severity=Severity.CRITICAL,
                    summary=(
                        f"The patch removes an authorization check from "
                        f"{line.path} without replacing it."
                    ),
                    excerpt=line.text.strip(),
                    path=line.path,
                )
            ]
    return []


def _authentication(added: list[DiffLine]) -> list[DetectedFinding]:
    return [
        DetectedFinding(
            category=FindingCategory.AUTHENTICATION_CHANGE,
            severity=Severity.HIGH,
            summary=f"The patch changes how requests authenticate in {line.path}.",
            excerpt=line.text.strip(),
            path=line.path,
        )
        for line in added
        if _AUTHENTICATION.search(line.text) and not contains_secret(line.text)
    ][:1]


def _scopes(added: list[DiffLine]) -> list[DetectedFinding]:
    return [
        DetectedFinding(
            category=FindingCategory.OAUTH_SCOPE_CHANGE,
            severity=Severity.HIGH,
            summary=f"The patch changes the OAuth scopes requested in {line.path}.",
            excerpt=line.text.strip(),
            path=line.path,
        )
        for line in added
        if _SCOPE.search(line.text)
    ][:1]


def _privilege(added: list[DiffLine]) -> list[DetectedFinding]:
    return [
        DetectedFinding(
            category=FindingCategory.PRIVILEGE_EXPANSION,
            severity=Severity.CRITICAL,
            summary=f"The patch widens a privilege or role in {line.path}.",
            excerpt=line.text.strip(),
            path=line.path,
        )
        for line in added
        if _PRIVILEGE.search(line.text)
    ][:1]


def _unsafe_parameters(added: list[DiffLine]) -> list[DetectedFinding]:
    return [
        DetectedFinding(
            category=FindingCategory.UNSAFE_PARAMETER,
            severity=Severity.HIGH,
            summary=(
                f"The patch introduces a construct that turns data into code in "
                f"{line.path}."
            ),
            excerpt=line.text.strip(),
            path=line.path,
        )
        for line in added
        if _UNSAFE_PARAMETER.search(line.text)
    ][:1]


def _dangerous_retry(added: list[DiffLine]) -> list[DetectedFinding]:
    return [
        DetectedFinding(
            category=FindingCategory.DANGEROUS_RETRY,
            severity=Severity.HIGH,
            summary=f"The patch adds a retry with no effective ceiling in {line.path}.",
            excerpt=line.text.strip(),
            path=line.path,
        )
        for line in added
        if _DANGEROUS_RETRY.search(line.text)
    ][:1]


def _duplicate_transaction(added: list[DiffLine]) -> list[DetectedFinding]:
    """A retried write with no idempotency key charges the customer twice.

    Only fires when the patch adds both a mutating call and a retry, and no
    idempotency key anywhere in the added lines — the combination is the risk,
    not either half.
    """
    text = "\n".join(line.text for line in added)
    has_retry = bool(re.search(r"retry|retries|backoff", text, re.IGNORECASE))
    has_mutation = bool(_MUTATING_CALL.search(text))

    if has_retry and has_mutation and not _IDEMPOTENCY.search(text):
        line = next(line for line in added if _MUTATING_CALL.search(line.text))
        return [
            DetectedFinding(
                category=FindingCategory.DUPLICATE_TRANSACTION_RISK,
                severity=Severity.HIGH,
                summary=(
                    f"The patch retries a mutating request in {line.path} with no "
                    "idempotency key. A retry can repeat the operation."
                ),
                excerpt=line.text.strip(),
                path=line.path,
            )
        ]
    return []


def _new_dependency(added: list[DiffLine]) -> list[DetectedFinding]:
    for line in added:
        if line.path and line.path.split("/")[-1] in _MANIFESTS and line.text.strip():
            return [
                DetectedFinding(
                    category=FindingCategory.NEW_DEPENDENCY,
                    severity=Severity.MEDIUM,
                    summary=(
                        f"The patch modifies the dependency manifest {line.path}. "
                        "The supply chain is part of the review surface."
                    ),
                    excerpt=line.text.strip(),
                    path=line.path,
                )
            ]
    return []


def _injection(added: list[DiffLine]) -> list[DetectedFinding]:
    return [
        DetectedFinding(
            category=FindingCategory.PROMPT_INJECTION_SUSPECTED,
            severity=Severity.HIGH,
            summary=(
                f"The patch adds instruction-shaped text to {line.path}. Provider "
                "documentation is untrusted input and may have carried it in."
            ),
            excerpt=line.text.strip(),
            path=line.path,
        )
        for line in added
        if _INJECTION.search(line.text)
    ][:1]


def _test_weakened(
    added: list[DiffLine], removed: list[DiffLine]
) -> list[DetectedFinding]:
    """A test file changed at all is a finding.

    Deliberately not "changed for the worse": judging that is the reviewer's
    job, and a migration that edits tests is something a person should see
    regardless (`02_ARCHITECTURE.md` §12).
    """
    touched = {
        line.path
        for line in (*added, *removed)
        if line.path and _TEST_PATH.search(line.path)
    }
    if not touched:
        return []

    path = sorted(touched)[0]
    removed_assertions = sum(
        1
        for line in removed
        if line.path in touched and re.search(r"\bassert\b|expect\(", line.text)
    )
    return [
        DetectedFinding(
            category=FindingCategory.TEST_WEAKENED,
            severity=Severity.HIGH if removed_assertions else Severity.MEDIUM,
            summary=(
                f"The patch modifies test files ({', '.join(sorted(touched))})"
                + (
                    f", removing {removed_assertions} assertion(s)."
                    if removed_assertions
                    else "."
                )
            ),
            excerpt=f"{len(touched)} test file(s) modified",
            path=path,
        )
    ]
