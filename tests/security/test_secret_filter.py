"""C3-01 acceptance: path exclusion and content redaction, both directions.

Path exclusion is the stronger of the two controls — a file that is never opened
cannot leak through a downstream bug — so most of these assert that specific
paths are refused *before* any read.

The negative tests matter as much. A filter that excludes ordinary source files
would make the whole product useless, and one that redacts every string would
push people to turn it off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.security.secret_filter import (
    ExclusionReason,
    classify_path,
    contains_secret,
    is_excluded_path,
    is_secret_path,
    redact,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_DOC = REPO_ROOT / "03_SECURITY_ACCESS.md"


# --- Every path pattern named in §2 -------------------------------------

SECRET_PATHS = [
    ".env",
    ".env.local",
    ".env.production",
    "config/.env",
    "deploy/prod.env",
    "keys/server.pem",
    "certs/private.key",
    "bundle.p12",
    "bundle.pfx",
    "server.crt",
    "app.keystore",
    "home/id_rsa",
    "home/id_rsa.pub",
    "home/id_ed25519",
    ".aws/credentials",
    "credentials",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    "secrets.yaml",
    "k8s/app-secrets.yml",
    "deploy/service-account-prod.json",
    ".ssh/known_hosts",
]


@pytest.mark.parametrize("path", SECRET_PATHS)
def test_secret_paths_are_excluded_and_flagged_as_secrets(path: str) -> None:
    assert is_excluded_path(path), f"{path} must never be opened"
    assert is_secret_path(path), f"{path} must be recorded as a secret exclusion"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".git/config", ExclusionReason.VCS_INTERNAL),
        ("node_modules/left-pad/index.js", ExclusionReason.DEPENDENCY_DIRECTORY),
        ("frontend/node_modules/react/index.js", ExclusionReason.DEPENDENCY_DIRECTORY),
        (".venv/lib/site.py", ExclusionReason.DEPENDENCY_DIRECTORY),
        (".next/static/chunk.js", ExclusionReason.BUILD_OUTPUT),
        ("dist/bundle.js", ExclusionReason.BUILD_OUTPUT),
        ("__pycache__/mod.cpython-312.pyc", ExclusionReason.BUILD_OUTPUT),
        ("assets/logo.png", ExclusionReason.BINARY_OR_MEDIA),
        ("docs/manual.pdf", ExclusionReason.BINARY_OR_MEDIA),
        ("data/app.sqlite3", ExclusionReason.BINARY_OR_MEDIA),
    ],
)
def test_noise_is_excluded_with_the_right_reason(path: str, reason: ExclusionReason) -> None:
    verdict = classify_path(path)

    assert verdict.excluded
    assert verdict.reason is reason
    # Noise is not a secret. Conflating them would inflate the security count
    # on the scan summary and make it meaningless.
    assert not is_secret_path(path)


# --- Negative: ordinary code must survive --------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "app/services/payment_service.py",
        "src/components/Checkout.tsx",
        "pyproject.toml",
        "package.json",
        "README.md",
        "tests/test_payments.py",
        "app/environment.py",          # contains "env" but is not a .env file
        "docs/development.md",
        "src/keyboard.ts",             # contains "key" but is not a key file
        "app/credentials_form.tsx",    # about credentials, does not hold one
    ],
)
def test_ordinary_source_files_are_readable(path: str) -> None:
    assert not is_excluded_path(path), f"{path} was wrongly excluded"


def test_exclusion_checks_every_path_segment() -> None:
    """A secret nested deep must be caught as readily as one at the root."""
    assert is_excluded_path("services/payments/config/.env")
    assert is_excluded_path("a/b/c/d/e/node_modules/pkg/index.js")


# --- The two halves are one control --------------------------------------


def test_the_filter_exposes_both_halves_from_one_import() -> None:
    """Callers must not have to remember two modules and pick correctly."""
    from backend.security import secret_filter

    assert callable(secret_filter.is_excluded_path)
    assert callable(secret_filter.redact)
    assert callable(secret_filter.contains_secret)


def test_content_redaction_still_applies_to_readable_files() -> None:
    """Path exclusion is the first line; redaction is the second."""
    from tests.support.secret_samples import GITHUB_TOKEN

    source = f'HEADERS = {{"Authorization": "{GITHUB_TOKEN}"}}'

    assert not is_excluded_path("app/client.py")
    assert contains_secret(source)
    assert GITHUB_TOKEN not in redact(source)


# --- Enforcement points (§2) ---------------------------------------------


def test_every_enforcement_point_invokes_the_filter() -> None:
    """§2 names five points. Each must actually call the filter.

    Checked by import and call-site inspection rather than behaviour, because
    the claim is structural: a point that stops calling the filter is a
    regression even if no test exercises that path yet.
    """
    checks = {
        # 1. Repository indexing — exclusion before reading.
        "backend/repository/indexer.py": ("classify_path", "is_secret_path"),
        # 1b. The source refuses excluded paths on read.
        "backend/repository/source.py": ("classify_path", "is_excluded_path"),
        # 2. Context retrieval — every slice redacted.
        "backend/repository/retrieval.py": ("redact",),
        # 3. Tool arguments and results.
        "backend/agents/tools/registry.py": ("redact",),
        # 4. Activity events and log records.
        "backend/observability/events.py": ("redact",),
        "backend/observability/logging.py": ("redact",),
    }

    for relative, expected in checks.items():
        source = (REPO_ROOT / relative).read_text()
        for symbol in expected:
            assert symbol in source, f"{relative} does not reference {symbol}"


def test_a_secret_bearing_file_cannot_reach_retrieved_context(tmp_path: Path) -> None:
    """The end-to-end guarantee: index a repo, retrieve, find no secret."""
    from backend.repository.indexer import build_index
    from backend.repository.local_adapter import LocalRepositoryAdapter
    from backend.repository.retrieval import ContextRetriever
    from tests.support.secret_samples import AWS_ACCESS_KEY_ID

    (tmp_path / "app").mkdir()
    (tmp_path / ".env").write_text(f"AWS_ACCESS_KEY_ID={AWS_ACCESS_KEY_ID}\n")
    (tmp_path / "app" / "client.py").write_text(
        f'import httpx\n\nKEY = "{AWS_ACCESS_KEY_ID}"\n\n'
        'def call():\n    return httpx.post("/v1/charges")\n'
    )

    source = LocalRepositoryAdapter(tmp_path)
    index = build_index(source)

    # The .env was never indexed at all.
    assert ".env" not in index.files
    assert any(entry.path == ".env" and entry.is_secret for entry in index.excluded)

    # And the secret hardcoded inside a *readable* source file is redacted on
    # the way out, because path exclusion cannot help there.
    retrieved = ContextRetriever(index, source, budget_bytes=100_000).for_call_sites("/v1/")
    assert AWS_ACCESS_KEY_ID not in retrieved.render()


def test_the_security_document_still_lists_the_patterns_this_enforces() -> None:
    """Guards against the doc and the filter drifting apart."""
    text = SECURITY_DOC.read_text()
    section = text.split("### Path exclusions", 1)[1].split("### Enforcement points", 1)[0]

    for marker in (".env", "*.pem", ".netrc", ".npmrc", "credentials"):
        assert marker in section, f"§2 no longer documents {marker}"


# --- Repository-declared secret rules (§2) -------------------------------


def test_secret_rules_are_adopted_from_a_repository_gitignore() -> None:
    """A project names its own secrets better than any static list can."""
    from backend.security.secret_filter import secret_patterns_from_gitignore

    patterns = secret_patterns_from_gitignore(
        "# build\ndist/\n*.log\nnode_modules/\n\n"
        "# secrets\ndeploy/live-credentials\n*.token\nMY_SECRETS/\n"
    )

    assert "deploy/live-credentials" in patterns
    assert "*.token" in patterns
    assert "MY_SECRETS" in patterns


def test_ordinary_ignore_rules_are_not_treated_as_secrets() -> None:
    """Adopting the whole file would make the secret count meaningless."""
    from backend.security.secret_filter import secret_patterns_from_gitignore

    patterns = secret_patterns_from_gitignore("dist/\n*.log\nnode_modules/\ncoverage/\n")

    assert patterns == []


def test_a_negation_rule_is_never_adopted_as_a_secret() -> None:
    """`!` re-includes a path; treating it as a secret would invert its meaning."""
    from backend.security.secret_filter import secret_patterns_from_gitignore

    assert secret_patterns_from_gitignore("!keep-this.env\n") == []


@pytest.mark.parametrize(
    "path",
    ["deploy/live-credentials", "api.token", "MY_SECRETS/db.yaml", "a/b/MY_SECRETS/x"],
)
def test_gitignore_rules_exclude_files_names_and_directories(path: str) -> None:
    """A rule may name a file, a full path, or a directory."""
    from backend.security.secret_filter import classify_path, secret_patterns_from_gitignore

    patterns = tuple(
        secret_patterns_from_gitignore("deploy/live-credentials\n*.token\nMY_SECRETS/\n")
    )

    verdict = classify_path(path, extra_secret_patterns=patterns)
    assert verdict.excluded
    assert verdict.reason is ExclusionReason.SECRET_PATH


def test_the_local_adapter_loads_the_repository_gitignore(tmp_path: Path) -> None:
    from backend.repository.local_adapter import LocalRepositoryAdapter

    (tmp_path / ".gitignore").write_text("deploy/live-credentials\n")
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "live-credentials").write_text("password=hunter2\n")
    (tmp_path / "app.py").write_text("x = 1\n")

    adapter = LocalRepositoryAdapter(tmp_path)
    listed = [f.path for f in adapter.list_files()]

    assert "deploy/live-credentials" in patterns_of(adapter)
    assert "deploy/live-credentials" not in listed
    assert "app.py" in listed


def patterns_of(adapter: object) -> tuple[str, ...]:
    return getattr(adapter, "extra_secret_patterns", ())


@pytest.mark.parametrize(
    "entry",
    [
        "monkey/",           # contains "key"
        "designtokens/",     # contains "token"
        "turkey/",           # contains "key"
        "keyboard/",         # starts with "key"
        "donkey-service/",   # contains "key"
        "environment/",      # contains "env"
        "certainly/",        # contains "cert"
        "authorship/",       # contains "auth"
        "passwordless-ui/",  # contains "password"
    ],
)
def test_ordinary_names_containing_a_secret_word_are_not_adopted(entry: str) -> None:
    """Regression: substring matching excluded real source directories.

    An earlier version matched trigger words as substrings, so `monkey/` was
    adopted as a secret rule and its whole directory vanished from analysis.
    Wrongly excluding source is the damaging direction — the code becomes
    invisible to the product rather than merely miscounted — so this is locked
    in with the real cases that broke it.
    """
    from backend.security.secret_filter import secret_patterns_from_gitignore

    assert secret_patterns_from_gitignore(entry) == []


@pytest.mark.parametrize(
    "entry",
    [
        "deploy/live-credentials",
        "*.token",
        "MY_SECRETS/",
        ".env",
        "*.pem",
        "config/api-keys.json",
        "secrets/",
        "prod.p12",
        "server.keystore",
    ],
)
def test_genuine_secret_rules_are_still_adopted(entry: str) -> None:
    """The other direction: tightening must not stop catching real secrets."""
    from backend.security.secret_filter import secret_patterns_from_gitignore

    assert secret_patterns_from_gitignore(entry), f"{entry} should be adopted"


def test_a_wrongly_excluded_directory_would_be_invisible_not_merely_miscounted() -> None:
    """States the asymmetry the tokenizer exists to protect.

    A secret wrongly *included* is redacted downstream. A source directory
    wrongly *excluded* is never indexed, never retrieved, and never analysed —
    so the product silently ignores part of the codebase.
    """
    import tempfile

    from backend.repository.indexer import build_index
    from backend.repository.local_adapter import LocalRepositoryAdapter

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / ".gitignore").write_text("monkey/\n")
        (root / "monkey").mkdir()
        (root / "monkey" / "patch.py").write_text("def go():\n    return 1\n")

        index = build_index(LocalRepositoryAdapter(root))

        assert "monkey/patch.py" in index.files, "ordinary source was excluded"


@pytest.mark.parametrize(
    "entry",
    [
        "authToken.json",
        "ApiKeyStore.js",
        "MyPassword.txt",
        "PrivateCertBundle/",
        "awsSecretKey",
    ],
)
def test_camel_case_secret_rules_are_adopted(entry: str) -> None:
    """Regression: whole-token matching initially missed camelCase entirely.

    A repository ignoring `authToken.json` means exactly what one ignoring
    `auth-token.json` means. Recognising only the hyphenated form left real
    secrets unfiltered — the dangerous direction — so all three naming styles
    are locked in here.
    """
    from backend.security.secret_filter import secret_patterns_from_gitignore

    assert secret_patterns_from_gitignore(entry), f"{entry} should be adopted"


@pytest.mark.parametrize("entry", ["envío/", "développement/", "Wörterbuch/", "résumé/"])
def test_non_ascii_names_are_not_torn_into_spurious_tokens(entry: str) -> None:
    """Regression: an ASCII-only split tore `envío/` into {env, o} and adopted it.

    Splitting must be Unicode-aware, or a trigger token gets matched from the
    wreckage of a word containing no English at all.
    """
    from backend.security.secret_filter import secret_patterns_from_gitignore

    assert secret_patterns_from_gitignore(entry) == []


def test_the_accepted_ambiguity_is_documented_where_it_lives() -> None:
    """`design-tokens/` and `token-ring/` ARE adopted, and that is deliberate.

    Lexically they are indistinguishable from `auth-tokens/`. Over-exclusion is
    the safer mistake, but it is a mistake, so it is asserted here rather than
    left to be discovered.
    """
    from backend.security import secret_filter

    assert secret_filter.secret_patterns_from_gitignore("design-tokens/") == ["design-tokens"]
    assert "accepted deliberately" in (secret_filter.secret_patterns_from_gitignore.__doc__ or "")
