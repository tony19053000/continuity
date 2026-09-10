"""Amazon Bedrock model provider.

This is the **only** module in the application that constructs a Strands model.
Everything else asks for one by agent role. A test walks the AST of
`backend/` and fails if `BedrockModel(` appears anywhere else, because a model
built at a call site is a model that escapes configuration, region resolution,
and per-role tuning.

Strands is the agent framework; Bedrock is the model behind it. Keeping the two
separated here is what lets a future provider be added without touching a single
agent.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from strands.models import BedrockModel, Model

from backend.models.enums import AgentRole
from backend.shared.config import BedrockConfig, Settings
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


class BedrockModelProvider:
    """The production provider. Wraps `strands.models.BedrockModel`.

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
    """Resolve the configured provider, or raise naming what is missing.

    Raising rather than returning a fallback is deliberate. A silent stand-in
    would let a run proceed and produce output that looks like agent reasoning
    but is not — exactly the fake-agent behaviour this project forbids.
    """
    bedrock = settings.bedrock
    if isinstance(bedrock, BedrockConfig):
        return BedrockModelProvider(bedrock)

    raise IntegrationNotConfigured("Amazon Bedrock", bedrock.reason)
