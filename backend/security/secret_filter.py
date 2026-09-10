"""Secret filtering: which files are never opened, and which text is redacted.

`03_SECURITY_ACCESS.md` §2 defines two halves of one control:

* **Path exclusion** — files that are never read at all. A `.env` that is never
  opened cannot leak through a bug in redaction downstream.
* **Content redaction** — patterns scrubbed from text already in hand. That half
  lives in `backend/shared/redaction.py` and is re-exported here so callers have
  a single import.

Order matters. Path exclusion runs *first*, during indexing, before any file
content exists in memory. Redaction is the second line, for content that must be
read (source files) but might still contain a credential.

The five enforcement points from §2:

1. Repository indexing — `is_excluded_path` (this module)
2. Context retrieval — `redact` on every slice
3. Tool arguments and results — the dispatcher
4. Activity events, audit rows, log records — the logging filter and emitter
5. Migration diffs — before a diff reaches a PR body or the frontend

`tests/security/test_secret_filter.py` asserts each point actually calls it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from backend.shared.redaction import (
    REDACTED,
    contains_secret,
    detected_secret_kinds,
    redact,
)

__all__ = [
    "REDACTED",
    "ExclusionReason",
    "PathVerdict",
    "classify_path",
    "contains_secret",
    "detected_secret_kinds",
    "is_excluded_path",
    "redact",
    "secret_patterns_from_gitignore",
]


class ExclusionReason(StrEnum):
    """Why a path was refused. Recorded so a scan can explain what it skipped."""

    # S105 suppressed: this names an exclusion category, not a credential.
    SECRET_PATH = "secret_path"  # noqa: S105
    VCS_INTERNAL = "vcs_internal"
    DEPENDENCY_DIRECTORY = "dependency_directory"
    BUILD_OUTPUT = "build_output"
    BINARY_OR_MEDIA = "binary_or_media"
    NOT_EXCLUDED = "not_excluded"


# --- Secret paths (§2). Never opened, never indexed, never retrieved. ---
SECRET_PATTERNS: Final[tuple[str, ...]] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "id_dsa*",
    "credentials",
    "credentials.*",
    ".git-credentials",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    "secrets.*",
    "*secrets.yaml",
    "*secrets.yml",
    "*.secret",
    "service-account*.json",
)

SECRET_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".aws", ".ssh", ".gnupg", ".docker", "secrets"}
)

# --- Non-secret exclusions: noise that would waste budget and add no signal ---
VCS_DIRECTORIES: Final[frozenset[str]] = frozenset({".git", ".hg", ".svn", ".bzr"})

DEPENDENCY_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        "node_modules",
        "vendor",
        "bower_components",
        ".venv",
        "venv",
        "env",
        "virtualenv",
        "site-packages",
        ".tox",
        ".nox",
    }
)

BUILD_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        ".next",
        "dist",
        "build",
        "out",
        "target",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".turbo",
        "coverage",
        "htmlcov",
        ".gradle",
        ".terraform",
    }
)

BINARY_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
        ".mp3", ".mp4", ".avi", ".mov", ".wav", ".webm",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".so", ".dll", ".dylib", ".a", ".o", ".class", ".jar",
        ".pyc", ".pyo", ".wasm", ".bin", ".exe",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        ".db", ".sqlite", ".sqlite3", ".parquet",
    }
)


# Tokens that make a `.gitignore` entry a *secret* rule rather than build noise.
#
# Matched as WHOLE TOKENS, never substrings. An earlier substring version
# adopted `monkey/` (contains "key") and `designtokens/` (contains "token") as
# secret rules, which excluded ordinary source directories from analysis
# entirely — the damaging direction, since a wrongly-excluded file is simply
# invisible to the product.
#
# Plurals are listed explicitly rather than stemmed, because stemming would
# reintroduce exactly the fuzziness this is here to remove.
_GITIGNORE_SECRET_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "secret", "secrets",
        "credential", "credentials",
        "password", "passwords", "passwd",
        "token", "tokens",
        "key", "keys", "keyfile", "keystore", "keypair",
        "apikey", "apikeys",
        "cert", "certs", "certificate", "certificates",
        "private",
        "auth",
        "env", "envs", "dotenv",
        "pem", "p12", "pfx", "jks", "asc", "gpg",
        # Unambiguous key types. "account" is deliberately absent — it is far
        # too common in ordinary names to carry a secret signal on its own, and
        # `service-account*.json` is already caught by the static list above.
        "rsa", "dsa", "ecdsa", "ed25519", "id_rsa",
        "htpasswd", "netrc", "npmrc", "pypirc",
    }
)

# Entries that look secret-ish but are ordinary tooling artifacts. Without
# these, `.env.example` — a file projects commit deliberately — would be adopted
# as a secret rule.
_GITIGNORE_FALSE_POSITIVES: Final = (
    ".env.example",
    ".env.sample",
    ".env.template",
    "example.env",
    "sample.env",
)

# Splitting has to handle three naming styles, and getting any of them wrong
# moves a secret into or out of scope:
#
#   deploy/live-credentials  → separators
#   MY_SECRETS               → underscores
#   authToken.json           → a case transition, with no separator at all
#
# `_CASE_BOUNDARY` inserts a separator before each lower→upper transition, so
# `AuthToken` decomposes to {auth, token} while single-case compounds such as
# `monkey` and `designtokens` stay whole and are correctly not adopted.
#
# The split itself is Unicode-aware (`\W`, not `[^a-z0-9]`). An ASCII-only split
# tore `envío/` into {env, o} and adopted it — matching a trigger token from the
# wreckage of a word that contains no English at all.
_CASE_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_SPLIT: Final = re.compile(r"[\W_]+", re.UNICODE)


def _tokenize(entry: str) -> set[str]:
    """Whole tokens in a `.gitignore` entry, across all three naming styles."""
    separated = _CASE_BOUNDARY.sub("-", entry)
    return {token for token in _TOKEN_SPLIT.split(separated.lower()) if token}


def secret_patterns_from_gitignore(content: str) -> list[str]:
    """Secret-looking ignore rules from a repository's own `.gitignore`.

    `03_SECURITY_ACCESS.md` §2 requires honouring these. A project knows its own
    naming conventions better than any static list does: if a team ignores
    `deploy/live-credentials`, that file almost certainly holds secrets even
    though nothing in its name matches our built-in patterns.

    Only rules containing a **whole** secret token are adopted. Taking the whole
    file would exclude `dist/` and `*.log` as "secrets" and make the count
    meaningless; substring matching would exclude `monkey/` and `designtokens/`,
    which is worse — a wrongly-excluded directory is invisible to the product
    rather than merely miscounted.

    A residual ambiguity remains and is accepted deliberately: a directory
    genuinely named `design-tokens/` tokenizes to `{design, tokens}` and is
    adopted. Lexically it is indistinguishable from `auth-tokens/`, and erring
    toward exclusion is the safer of the two mistakes.

    Three naming styles are handled — separators, underscores, and camelCase —
    because a repository that ignores `authToken.json` means exactly what one
    ignoring `auth-token.json` means, and only recognising the hyphenated form
    would miss real secrets. See `_tokenize`.
    """
    patterns: list[str] = []

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):  # a negation re-includes; never a secret rule
            continue

        entry = line.rstrip("/").lstrip("/")
        if not entry:
            continue

        lowered = entry.lower()
        if any(fp in lowered for fp in _GITIGNORE_FALSE_POSITIVES):
            continue

        if not (_tokenize(entry) & _GITIGNORE_SECRET_TOKENS):
            continue

        patterns.append(entry)

    return patterns


@dataclass(frozen=True, slots=True)
class PathVerdict:
    excluded: bool
    reason: ExclusionReason
    detail: str = ""


def classify_path(
    relative_path: str, *, extra_secret_patterns: tuple[str, ...] = ()
) -> PathVerdict:
    """Decide whether `relative_path` may be read, and say why not.

    Takes a repository-relative POSIX path. Every segment is checked, not just
    the final name, so `frontend/node_modules/x.js` and `config/.env` are caught
    as readily as a top-level match.

    `extra_secret_patterns` carries rules derived from the repository's own
    `.gitignore` (see `secret_patterns_from_gitignore`).
    """
    path = PurePosixPath(relative_path)
    parts = path.parts
    name = path.name

    for segment in parts[:-1] if len(parts) > 1 else ():
        if segment in SECRET_DIRECTORIES:
            return PathVerdict(True, ExclusionReason.SECRET_PATH, f"directory {segment!r}")
        if segment in VCS_DIRECTORIES:
            return PathVerdict(True, ExclusionReason.VCS_INTERNAL, f"directory {segment!r}")
        if segment in DEPENDENCY_DIRECTORIES:
            return PathVerdict(
                True, ExclusionReason.DEPENDENCY_DIRECTORY, f"directory {segment!r}"
            )
        if segment in BUILD_DIRECTORIES:
            return PathVerdict(True, ExclusionReason.BUILD_OUTPUT, f"directory {segment!r}")

    # A directory name may also be the final segment when walking directories.
    if name in SECRET_DIRECTORIES:
        return PathVerdict(True, ExclusionReason.SECRET_PATH, f"directory {name!r}")
    if name in VCS_DIRECTORIES:
        return PathVerdict(True, ExclusionReason.VCS_INTERNAL, f"directory {name!r}")
    if name in DEPENDENCY_DIRECTORIES:
        return PathVerdict(True, ExclusionReason.DEPENDENCY_DIRECTORY, f"directory {name!r}")
    if name in BUILD_DIRECTORIES:
        return PathVerdict(True, ExclusionReason.BUILD_OUTPUT, f"directory {name!r}")

    for pattern in SECRET_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return PathVerdict(True, ExclusionReason.SECRET_PATH, f"matches {pattern!r}")

    # Repository-declared secret rules. A `.gitignore` entry may name a file, a
    # full path, or a directory, so all three are checked — matching only the
    # bare name would let `MY_SECRETS/token.txt` through because the rule names
    # the directory rather than the file.
    for pattern in extra_secret_patterns:
        matches_name = fnmatch.fnmatch(name, pattern)
        matches_path = fnmatch.fnmatch(relative_path, pattern)
        matches_segment = any(fnmatch.fnmatch(segment, pattern) for segment in parts)
        if matches_name or matches_path or matches_segment:
            return PathVerdict(
                True, ExclusionReason.SECRET_PATH, f"matches .gitignore rule {pattern!r}"
            )

    if path.suffix.lower() in BINARY_SUFFIXES:
        return PathVerdict(True, ExclusionReason.BINARY_OR_MEDIA, f"suffix {path.suffix!r}")

    return PathVerdict(False, ExclusionReason.NOT_EXCLUDED)


def is_excluded_path(
    relative_path: str, *, extra_secret_patterns: tuple[str, ...] = ()
) -> bool:
    """Whether this path must never be opened."""
    return classify_path(relative_path, extra_secret_patterns=extra_secret_patterns).excluded


def is_secret_path(
    relative_path: str, *, extra_secret_patterns: tuple[str, ...] = ()
) -> bool:
    """Whether the exclusion is specifically a *secret* rather than noise.

    Distinguished because the two have different consequences: skipping
    `node_modules` is housekeeping, while skipping `.env` is a security control
    worth surfacing on the scan summary.
    """
    return (
        classify_path(relative_path, extra_secret_patterns=extra_secret_patterns).reason
        is ExclusionReason.SECRET_PATH
    )
