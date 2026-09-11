"""Application configuration.

Two rules govern this module, both from `03_SECURITY_ACCESS.md`:

1. **Nothing is invented.** A missing credential produces `NotConfigured`, never
   a placeholder that lets code proceed as though the integration works.
2. **Secrets never render.** Every secret field is a `SecretStr`, so no
   `repr()`, log record, or traceback can print one.

Optional integrations (Bedrock, GitHub App, Google OAuth) are exposed as
*groups*: either the whole group is configured, or the group is `NotConfigured`.
That prevents half-configured states where three of four variables are present
and the failure surfaces much later as a confusing runtime error.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Primary model. Verified present in the live `models.list()` for this API key
# and confirmed to drive Strands tool calling end to end (C2-07). Overridable
# via GEMINI_MODEL; never hardwired at a call site.
DEFAULT_GEMINI_MODEL: Final = "gemini-2.5-flash"

# Optional future provider. Kept because the abstraction is already clean, not
# because it is in use — see `02_ARCHITECTURE.md` §2.
DEFAULT_BEDROCK_MODEL_ID: Final = "global.anthropic.claude-sonnet-4-6"


class NotConfigured:
    """Sentinel for an integration whose configuration is absent.

    Truthy checks fail, so `if settings.github_app:` reads naturally and a
    caller cannot accidentally treat an unconfigured integration as usable.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __bool__(self) -> Literal[False]:
        return False

    def __repr__(self) -> str:
        return f"NotConfigured({self.reason!r})"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class GeminiConfig:
    """Resolved Google Gemini configuration.

    The API key is a `SecretStr` and is unwrapped only when the client is
    constructed, so it cannot be rendered by a repr or a log record.
    """

    __slots__ = ("api_key", "model")

    def __init__(self, api_key: SecretStr, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def __repr__(self) -> str:
        return f"GeminiConfig(model={self.model!r})"


class BedrockConfig:
    """Resolved Amazon Bedrock configuration.

    Holds no credentials: AWS authentication comes from the standard credential
    chain (environment, shared config, or an instance/task role), never from
    Continuity's own settings.
    """

    __slots__ = ("model_id", "region")

    def __init__(self, region: str, model_id: str) -> None:
        self.region = region
        self.model_id = model_id

    def __repr__(self) -> str:
        return f"BedrockConfig(region={self.region!r}, model_id={self.model_id!r})"


class GitHubAppConfig:
    __slots__ = ("app_id", "client_id", "client_secret", "private_key_path", "webhook_secret")

    def __init__(
        self,
        app_id: str,
        client_id: str,
        client_secret: SecretStr,
        private_key_path: Path,
        webhook_secret: SecretStr | None,
    ) -> None:
        self.app_id = app_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.private_key_path = private_key_path
        self.webhook_secret = webhook_secret

    def __repr__(self) -> str:
        # Secrets are SecretStr, which renders as '**********'.
        return f"GitHubAppConfig(app_id={self.app_id!r}, client_id={self.client_id!r})"


class GoogleOAuthConfig:
    __slots__ = ("client_id", "client_secret", "session_secret")

    def __init__(
        self, client_id: str, client_secret: SecretStr, session_secret: SecretStr
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session_secret = session_secret

    def __repr__(self) -> str:
        return f"GoogleOAuthConfig(client_id={self.client_id!r})"


class Settings(BaseSettings):
    """Environment-backed settings.

    Field names match environment variable names exactly; a test in
    `tests/unit/shared/test_config.py` asserts this file and `.env.example`
    stay in sync, so a new variable cannot be added to one and forgotten in the
    other.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, values: Any) -> Any:
        """Treat an empty value as absent, so the field default applies.

        `.env.example` documents every variable with a blank value, and copying
        it to `.env` is the documented way to start. Without this, that copy
        fails at startup on the first non-string field — `AGENTCORE_OBSERVABILITY_ENABLED=`
        is not a valid boolean, and `PROVIDER_POLL_INTERVAL_SECONDS=` is not a
        valid int — which makes the documented setup path broken by default.

        A blank line in an env file means "I have not set this", and that is
        what it now means here.
        """
        if not isinstance(values, dict):
            return values
        return {
            key: value
            for key, value in values.items()
            if not (isinstance(value, str) and not value.strip())
        }

    # ---- Application -----------------------------------------------------
    CONTINUITY_ENV: Environment = Environment.DEVELOPMENT
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Origin the browser loads the frontend from. The API sends credentials, so
    # this must be an explicit origin — never a wildcard.
    FRONTEND_ORIGIN: str = ""

    # ---- Persistence -----------------------------------------------------
    # Empty in development means "use the local SQLite file"; see
    # `database_url`. In production it is required and startup fails without it.
    DATABASE_URL: str = ""

    # ---- Primary LLM: Google Gemini via Strands --------------------------
    GEMINI_API_KEY: SecretStr = SecretStr("")
    GEMINI_MODEL: str = ""

    # ---- AWS ------------------------------------------------------------
    # Credentials come from the standard AWS chain (profile, environment, or an
    # instance/task role). No AWS key ever appears in Continuity's own config.
    AWS_PROFILE: str = ""
    AWS_REGION: str = ""

    # ---- Optional future model provider: Amazon Bedrock ------------------
    BEDROCK_MODEL_ID: str = ""

    # ---- AgentCore (Phase 8) --------------------------------------------
    AGENTCORE_RUNTIME_ARN: str = ""
    AGENTCORE_MEMORY_ID: str = ""
    AGENTCORE_GATEWAY_URL: str = ""
    AGENTCORE_OBSERVABILITY_ENABLED: bool = False

    # ---- GitHub App ------------------------------------------------------
    GITHUB_APP_ID: str = ""
    GITHUB_APP_CLIENT_ID: str = ""
    GITHUB_APP_CLIENT_SECRET: SecretStr = SecretStr("")
    GITHUB_APP_PRIVATE_KEY_PATH: str = ""
    GITHUB_APP_WEBHOOK_SECRET: SecretStr = SecretStr("")

    # ---- User authentication --------------------------------------------
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: SecretStr = SecretStr("")
    SESSION_SECRET: SecretStr = SecretStr("")

    # ---- Agent runtime limits -------------------------------------------
    MAX_REPAIR_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    AGENT_MAX_OUTPUT_ATTEMPTS: int = Field(default=2, ge=1, le=10)
    EXECUTION_TIMEOUT_SECONDS: int = Field(default=300, ge=1)
    EXECUTION_MAX_OUTPUT_BYTES: int = Field(default=1_048_576, ge=1024)
    CONTEXT_BUDGET_BYTES: int = Field(default=200_000, ge=1024)

    # ---- Migration workspaces -------------------------------------------
    #: Where isolated migration worktrees are created. Empty means a directory
    #: under the system temp root, chosen at startup — a path, not a credential,
    #: so a default is a documented convenience rather than an invented value.
    WORKSPACE_ROOT: str = ""
    WORKSPACE_MAX_AGE_SECONDS: int = Field(default=86_400, ge=60)

    # ---- Provider monitoring --------------------------------------------
    #: Whether the in-process scheduler starts with the application. Off in
    #: tests and for one-off API processes; on is the product's normal state,
    #: because monitoring nobody started is the gap this switch exists to make
    #: visible rather than silent.
    SCHEDULER_ENABLED: bool = True
    PROVIDER_POLL_INTERVAL_SECONDS: int = Field(default=3600, ge=60)
    PROVIDER_FETCH_TIMEOUT_SECONDS: int = Field(default=30, ge=1)

    # ------------------------------------------------------------------
    # Derived configuration
    # ------------------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Resolved database URL.

        Development and test fall back to a local SQLite file — a path, not a
        credential, so this is a documented default rather than an invented
        value. Production requires an explicit URL.
        """
        if self.DATABASE_URL:
            return self.DATABASE_URL
        if self.CONTINUITY_ENV is Environment.PRODUCTION:
            raise ConfigurationError("DATABASE_URL")
        return "sqlite+aiosqlite:///./data/continuity.db"

    @property
    def frontend_origin(self) -> str:
        """Resolved browser origin for the frontend.

        The API is served on a different port from the Next.js dev server, so a
        browser request from the frontend is cross-origin and blocked without
        CORS. Development defaults to the standard Next.js port — a public
        localhost URL, not a credential — while production must state its origin
        explicitly, because getting this wrong there means either a broken app
        or an over-permissive one.
        """
        if self.FRONTEND_ORIGIN:
            return self.FRONTEND_ORIGIN
        if self.CONTINUITY_ENV is Environment.PRODUCTION:
            raise ConfigurationError(
                "FRONTEND_ORIGIN",
                "required in production so CORS is not guessed",
            )
        return "http://localhost:3000"

    @property
    def gemini(self) -> GeminiConfig | NotConfigured:
        """Primary model configuration, or why it is unusable."""
        if not self.GEMINI_API_KEY.get_secret_value():
            return NotConfigured("GEMINI_API_KEY is not set")
        return GeminiConfig(
            api_key=self.GEMINI_API_KEY,
            model=self.GEMINI_MODEL or DEFAULT_GEMINI_MODEL,
        )

    @property
    def bedrock(self) -> BedrockConfig | NotConfigured:
        """Bedrock configuration, or why it is unusable.

        Credentials are deliberately not checked here: they come from the AWS
        credential chain and their absence surfaces on the first real call.
        This reports only what Continuity itself must be told.
        """
        if not self.AWS_REGION:
            return NotConfigured("AWS_REGION is not set")
        return BedrockConfig(
            region=self.AWS_REGION,
            model_id=self.BEDROCK_MODEL_ID or DEFAULT_BEDROCK_MODEL_ID,
        )

    @property
    def github_app(self) -> GitHubAppConfig | NotConfigured:
        missing = [
            name
            for name, value in (
                ("GITHUB_APP_ID", self.GITHUB_APP_ID),
                ("GITHUB_APP_CLIENT_ID", self.GITHUB_APP_CLIENT_ID),
                ("GITHUB_APP_CLIENT_SECRET", self.GITHUB_APP_CLIENT_SECRET.get_secret_value()),
                ("GITHUB_APP_PRIVATE_KEY_PATH", self.GITHUB_APP_PRIVATE_KEY_PATH),
            )
            if not value
        ]
        if missing:
            return NotConfigured(f"missing: {', '.join(missing)}")

        key_path = Path(self.GITHUB_APP_PRIVATE_KEY_PATH)
        if not key_path.is_file():
            return NotConfigured(f"GITHUB_APP_PRIVATE_KEY_PATH does not exist: {key_path}")

        webhook_secret = self.GITHUB_APP_WEBHOOK_SECRET
        return GitHubAppConfig(
            app_id=self.GITHUB_APP_ID,
            client_id=self.GITHUB_APP_CLIENT_ID,
            client_secret=self.GITHUB_APP_CLIENT_SECRET,
            private_key_path=key_path,
            webhook_secret=webhook_secret if webhook_secret.get_secret_value() else None,
        )

    @property
    def google_oauth(self) -> GoogleOAuthConfig | NotConfigured:
        missing = [
            name
            for name, value in (
                ("GOOGLE_OAUTH_CLIENT_ID", self.GOOGLE_OAUTH_CLIENT_ID),
                ("GOOGLE_OAUTH_CLIENT_SECRET", self.GOOGLE_OAUTH_CLIENT_SECRET.get_secret_value()),
                ("SESSION_SECRET", self.SESSION_SECRET.get_secret_value()),
            )
            if not value
        ]
        if missing:
            return NotConfigured(f"missing: {', '.join(missing)}")
        return GoogleOAuthConfig(
            client_id=self.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=self.GOOGLE_OAUTH_CLIENT_SECRET,
            session_secret=self.SESSION_SECRET,
        )

    def __repr__(self) -> str:
        """Redacted representation.

        Only non-sensitive operational fields are shown. Every secret is a
        `SecretStr` and is omitted entirely rather than rendered as a mask,
        so there is nothing to accidentally unwrap.
        """
        return (
            f"Settings(env={self.CONTINUITY_ENV.value}, "
            f"log_level={self.LOG_LEVEL}, "
            f"gemini={'configured' if self.gemini else 'not_configured'}, "
            f"bedrock={'configured' if self.bedrock else 'not_configured'}, "
            f"github_app={'configured' if self.github_app else 'not_configured'}, "
            f"google_oauth={'configured' if self.google_oauth else 'not_configured'})"
        )

    __str__ = __repr__


class ConfigurationError(RuntimeError):
    """Raised at startup when required configuration is absent.

    The message names the exact variable so the operator does not have to guess.
    """

    def __init__(self, variable: str, detail: str | None = None) -> None:
        self.variable = variable
        message = f"Required configuration is missing: {variable}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, loaded once.

    Cached so that `.env` is read a single time. Tests clear the cache via
    `get_settings.cache_clear()` rather than mutating a global.
    """
    return Settings()
