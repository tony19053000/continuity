"""C2-07: prove Strands genuinely drives an agent and returns validated state.

This is the mandatory-technology test. It uses a real `strands.Agent` against a
real Amazon Bedrock model, takes the validated Pydantic result, and feeds it
into a real state transition — the full
`agent → structured output → deterministic transition` path.

**It never passes without Bedrock.** When AWS is unconfigured it *skips with the
exact blocker named*, so a green suite can never be mistaken for a working model
integration. `STATUS.md` blocker B-01 records the same thing.

Run it once credentials exist:

    AWS_REGION=us-west-2 uv run pytest tests/integration/test_strands_roundtrip.py -v
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, Field

from backend.agents.base import AgentContract, ContinuityAgent
from backend.models import RunState, StateTransition
from backend.models.enums import AgentRole
from backend.models.schemas import TransitionEvidence
from backend.models.session import session_scope
from backend.orchestration.state_machine import transition
from backend.shared.config import BedrockConfig, Settings
from backend.shared.model_provider import build_model_provider

pytestmark = pytest.mark.requires_bedrock


def _bedrock_or_skip() -> BedrockConfig:
    """Resolve Bedrock configuration, or skip naming exactly what is missing."""
    settings = Settings()
    bedrock = settings.bedrock
    if not isinstance(bedrock, BedrockConfig):
        pytest.skip(
            "SKIPPED, NOT PASSED — Amazon Bedrock is not configured: "
            f"{bedrock.reason}. This is STATUS.md blocker B-01. Strands agent "
            "execution is unproven until this test runs. Set AWS_REGION and "
            "provide AWS credentials with Bedrock model access."
        )
    return bedrock


class ClassificationTask(BaseModel):
    changelog_line: str


class Classification(BaseModel):
    """The structured contract the model must satisfy."""

    breaking: bool = Field(description="Whether this change can break a caller")
    resource: str = Field(description="The endpoint, field, or event affected")
    confidence_note: str = Field(max_length=300)


class MiniScout(ContinuityAgent[ClassificationTask, Classification]):
    """A minimal real agent, deliberately unambiguous so the test is stable."""

    contract = AgentContract(
        role=AgentRole.CHANGE_SCOUT,
        system_prompt=(
            "You classify a single provider changelog entry. Decide whether it "
            "is breaking for existing callers and name the affected resource. "
            "Return only the structured output."
        ),
        allowed_tools=frozenset(),
        input_model=ClassificationTask,
        output_model=Classification,
        max_attempts=2,
    )

    def build_prompt(self, task: ClassificationTask) -> str:
        return f"Changelog entry: {task.changelog_line}"


async def test_strands_returns_validated_output_that_drives_a_transition(
    database: None,
) -> None:
    """The full path: Strands → Bedrock → validated Pydantic → state change."""
    _bedrock_or_skip()

    provider = build_model_provider(Settings())
    agent = MiniScout(provider)

    result = await agent.run(
        ClassificationTask(
            changelog_line=(
                "The webhook event `payment.paid` has been renamed to "
                "`payment.succeeded`. The old name is no longer emitted."
            )
        )
    )

    # Validated structure, not text. If Strands returned prose, `agent.run`
    # would have raised rather than reaching here.
    assert isinstance(result, Classification)
    assert result.breaking is True
    assert "payment" in result.resource.lower()

    # And that structure drives a real, legal transition.
    async with session_scope() as session:
        record = await transition(
            session,
            from_state=RunState.CHANGE_ANALYSIS_RUNNING,
            to_state=RunState.CHANGE_ANALYSIS_COMPLETE,
            evidence=TransitionEvidence(
                reason=f"Classified {result.resource} as breaking={result.breaking}",
                actor=AgentRole.CHANGE_SCOUT.value,
                detail={"breaking": result.breaking, "resource": result.resource},
            ),
            migration_run_id=None,
        )
        record_id = record.id

    async with session_scope() as session:
        stored = await session.get(StateTransition, record_id)

    assert stored is not None
    assert stored.to_state is RunState.CHANGE_ANALYSIS_COMPLETE
    assert stored.actor == AgentRole.CHANGE_SCOUT.value


async def test_the_model_provider_reports_the_configured_model(database: None) -> None:
    """Guards against silently running on an unexpected model."""
    bedrock = _bedrock_or_skip()

    provider = build_model_provider(Settings())

    assert provider.model_id == bedrock.model_id
    assert uuid.UUID(int=0)  # sanity: the test body executed
