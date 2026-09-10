"""Model providers.

This is the **only** module in the application that constructs a Strands model.
Everything else asks for one by agent role, because a model built at a call site
escapes configuration and per-role tuning.

Two tests in `tests/security/test_agent_boundaries.py` enforce that: one checks
each known constructor appears only here, and one — the rule that cannot go
stale — asserts no other module imports from `strands.models` at all. The second
exists because the first is an enumeration, and an enumeration was exactly what
went wrong when Gemini arrived: the guard listed `BedrockModel` only, leaving the
provider actually carrying live traffic unguarded.

**Strands is the agent framework; the model is swappable behind it.** That
separation is the reason this change was a new class and a config group rather
than a rewrite: no agent, contract, prompt, or test needed to change when the
primary model moved from Bedrock to Gemini.

Primary provider: **Google Gemini** (`GeminiModelProvider`).
Optional future provider: **Amazon Bedrock** (`BedrockModelProvider`) — retained
because the abstraction is already clean and AgentCore remains the production
agent infrastructure target, not because it is in use.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from strands.models import BedrockModel, Model
from strands.models.gemini import GeminiModel

from backend.models.enums import AgentRole
from backend.shared.config import BedrockConfig, GeminiConfig, Settings
from backend.shared.errors import IntegrationNotConfigured

# Temperature per role, chosen for the job rather than uniformly.
#
# The agents that must not invent things run at 0.0: a changelog either says a
# field was removed or it does not, and a test either passed or it did not.
# Migration Engineer sits slightly higher because writing a patch benefits from
# some flexibility when the first approach does not fit the surrounding code.
ROLE_TEMPERATURE: dict[AgentRole, float] = {
    AgentRole.ORCHESTRATOR: 0.0,
    AgentRole.CHANGE_SCOUT: 0.0,
    AgentRole.INTEGRATION_MAPPER: 0.1,
    AgentRole.IMPACT_ANALYST: 0.0,
    AgentRole.MIGRATION_ENGINEER: 0.2,
    AgentRole.VALIDATOR: 0.0,
    AgentRole.SECURITY_REVIEWER: 0.0,
    AgentRole.RED_TEAM: 0.4,
    AgentRole.RELEASE_GUARDIAN: 0.0,
}

DEFAULT_TEMPERATURE = 0.0


@runtime_checkable
class ModelProvider(Protocol):
    """Builds a configured model for a given agent role."""

    def build_model(self, role: AgentRole) -> Model: ...

    @property
    def model_id(self) -> str: ...


class GeminiModelProvider:
    """The primary provider. Wraps `strands.models.gemini.GeminiModel`.

    The API key is unwrapped from its `SecretStr` exactly here, at the point the
    client is built, and is never stored on the provider — so a provider caught
    in a traceback or a log record carries nothing sensitive.
    """

    def __init__(self, config: GeminiConfig) -> None:
        self._config = config

    @property
    def model_id(self) -> str:
        return self._config.model

    def build_model(self, role: AgentRole) -> Model:
        return GeminiModel(
            client_args={"api_key": self._config.api_key.get_secret_value()},
            model_id=self._config.model,
            params={"temperature": ROLE_TEMPERATURE.get(role, DEFAULT_TEMPERATURE)},
        )

    def __repr__(self) -> str:
        return f"GeminiModelProvider(model={self._config.model!r})"


class BedrockModelProvider:
    """Optional future provider. Wraps `strands.models.BedrockModel`.

    Holds no credentials: AWS authentication comes from the standard credential
    chain (environment, shared config, or an instance/task role). The only thing
    Continuity supplies is which model and which region.
    """

    def __init__(self, config: BedrockConfig) -> None:
        self._config = config

    @property
    def model_id(self) -> str:
        return self._config.model_id

    @property
    def region(self) -> str:
        return self._config.region

    def build_model(self, role: AgentRole) -> Model:
        return BedrockModel(
            model_id=self._config.model_id,
            region_name=self._config.region,
            temperature=ROLE_TEMPERATURE.get(role, DEFAULT_TEMPERATURE),
        )

    def __repr__(self) -> str:
        return f"BedrockModelProvider(model_id={self._config.model_id!r}, region={self._config.region!r})"


def build_model_provider(settings: Settings) -> ModelProvider:
    """Resolve the active provider, or raise naming what is missing.

    Gemini is primary. Bedrock is used only if Gemini is unconfigured *and*
    Bedrock is, which keeps the fallback explicit rather than accidental.

    Raising rather than returning a stand-in is deliberate. A silent fallback
    would let a run proceed and produce output that looks like agent reasoning
    but is not — exactly the fake-agent behaviour this project forbids.
    """
    gemini = settings.gemini
    if isinstance(gemini, GeminiConfig):
        return GeminiModelProvider(gemini)

    bedrock = settings.bedrock
    if isinstance(bedrock, BedrockConfig):
        return BedrockModelProvider(bedrock)

    raise IntegrationNotConfigured(
        "Google Gemini", f"{gemini.reason}; Amazon Bedrock is also unavailable: {bedrock.reason}"
    )
