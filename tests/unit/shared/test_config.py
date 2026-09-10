"""C1-02 acceptance: configuration is honest, redacted, and in sync with .env.example."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.shared.config import (
    DEFAULT_BEDROCK_MODEL_ID,
    BedrockConfig,
    ConfigurationError,
    Environment,
    GitHubAppConfig,
    GoogleOAuthConfig,
    NotConfigured,
    Settings,
)
from tests.support.secret_samples import GITHUB_SERVER_TOKEN

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# Variables consumed by the Next.js frontend, not by the Python Settings model.
FRONTEND_PREFIX = "NEXT_PUBLIC_"


def _env_example_keys() -> set[str]:
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
    return set(pattern.findall(ENV_EXAMPLE.read_text()))


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# --- .env.example sync ---------------------------------------------------


def test_every_backend_variable_in_env_example_exists_in_settings() -> None:
    """A variable documented for operators must actually be read."""
    documented = {k for k in _env_example_keys() if not k.startswith(FRONTEND_PREFIX)}
    known = set(Settings.model_fields)

    assert not documented - known, f"documented but unused: {sorted(documented - known)}"


def test_every_settings_field_is_documented_in_env_example() -> None:
    """A variable the code reads must be discoverable by an operator."""
    documented = _env_example_keys()
    known = set(Settings.model_fields)

    assert not known - documented, f"undocumented settings: {sorted(known - documented)}"


def test_env_example_contains_no_secret_values() -> None:
    """Non-sensitive defaults are allowed; secrets and credentials are not."""
    from backend.shared.redaction import contains_secret

    assert not contains_secret(ENV_EXAMPLE.read_text())

    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if any(marker in key for marker in ("SECRET", "PASSWORD", "TOKEN", "KEY", "CLIENT_ID")):
            assert value == "", f"{key} must be blank in .env.example, found a value"


# --- Redaction -----------------------------------------------------------


def test_repr_and_str_never_render_a_secret() -> None:
    settings = _settings(
        GITHUB_APP_CLIENT_SECRET=GITHUB_SERVER_TOKEN,
        GOOGLE_OAUTH_CLIENT_SECRET="google-oauth-secret-value",
        SESSION_SECRET="session-secret-value",
        GITHUB_APP_WEBHOOK_SECRET="webhook-secret-value",
    )

    for rendered in (repr(settings), str(settings)):
        assert GITHUB_SERVER_TOKEN not in rendered
        assert "google-oauth-secret-value" not in rendered
        assert "session-secret-value" not in rendered
        assert "webhook-secret-value" not in rendered


def test_secret_fields_do_not_leak_through_model_dump() -> None:
    settings = _settings(SESSION_SECRET="session-secret-value")

    assert "session-secret-value" not in str(settings.model_dump())


# --- NotConfigured -------------------------------------------------------


def test_bedrock_is_not_configured_without_a_region() -> None:
    result = _settings().bedrock

    assert isinstance(result, NotConfigured)
    assert not result
    assert "AWS_REGION" in result.reason


def test_bedrock_uses_the_documented_default_model() -> None:
    result = _settings(AWS_REGION="us-west-2").bedrock

    assert isinstance(result, BedrockConfig)
    assert result.model_id == DEFAULT_BEDROCK_MODEL_ID


def test_bedrock_model_id_is_overridable() -> None:
    result = _settings(AWS_REGION="us-west-2", BEDROCK_MODEL_ID="some.other.model").bedrock

    assert isinstance(result, BedrockConfig)
    assert result.model_id == "some.other.model"


def test_github_app_reports_every_missing_variable() -> None:
    result = _settings(GITHUB_APP_ID="12345").github_app

    assert isinstance(result, NotConfigured)
    assert "GITHUB_APP_CLIENT_ID" in result.reason
    assert "GITHUB_APP_CLIENT_SECRET" in result.reason
    assert "GITHUB_APP_PRIVATE_KEY_PATH" in result.reason
    # A variable that IS set must not be reported as missing.
    assert "GITHUB_APP_ID" not in result.reason.replace("GITHUB_APP_ID_", "")


def test_github_app_is_not_configured_when_the_key_file_is_absent(tmp_path: Path) -> None:
    """A path that does not exist is worse than a blank one — fail loudly now."""
    result = _settings(
        GITHUB_APP_ID="12345",
        GITHUB_APP_CLIENT_ID="Iv1.abc",
        GITHUB_APP_CLIENT_SECRET="a-secret",
        GITHUB_APP_PRIVATE_KEY_PATH=str(tmp_path / "missing.pem"),
    ).github_app

    assert isinstance(result, NotConfigured)
    assert "does not exist" in result.reason


def test_github_app_is_configured_when_complete(tmp_path: Path) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text("not-a-real-key")

    result = _settings(
        GITHUB_APP_ID="12345",
        GITHUB_APP_CLIENT_ID="Iv1.abc",
        GITHUB_APP_CLIENT_SECRET="a-secret",
        GITHUB_APP_PRIVATE_KEY_PATH=str(key_file),
    ).github_app

    assert isinstance(result, GitHubAppConfig)
    assert bool(result)
    assert result.webhook_secret is None


def test_google_oauth_requires_all_three_variables() -> None:
    partial = _settings(GOOGLE_OAUTH_CLIENT_ID="client-id").google_oauth
    assert isinstance(partial, NotConfigured)

    complete = _settings(
        GOOGLE_OAUTH_CLIENT_ID="client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="client-secret",
        SESSION_SECRET="session-secret",
    ).google_oauth
    assert isinstance(complete, GoogleOAuthConfig)


# --- Required configuration ---------------------------------------------


def test_production_requires_an_explicit_database_url() -> None:
    settings = _settings(CONTINUITY_ENV=Environment.PRODUCTION)

    with pytest.raises(ConfigurationError) as excinfo:
        _ = settings.database_url

    assert excinfo.value.variable == "DATABASE_URL"
    assert "DATABASE_URL" in str(excinfo.value)


def test_development_falls_back_to_local_sqlite() -> None:
    assert _settings().database_url.startswith("sqlite+aiosqlite:///")


# --- Malformed configuration --------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MAX_REPAIR_ATTEMPTS", 0),
        ("MAX_REPAIR_ATTEMPTS", 99),
        ("EXECUTION_TIMEOUT_SECONDS", 0),
        ("CONTEXT_BUDGET_BYTES", 10),
        ("LOG_LEVEL", "CHATTY"),
        ("CONTINUITY_ENV", "staging"),
        ("PROVIDER_POLL_INTERVAL_SECONDS", 5),
    ],
)
def test_malformed_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})
