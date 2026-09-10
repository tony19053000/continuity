"""C2-07: prove Strands genuinely drives a live model, a tool, and a transition.

This is the mandatory-technology test. It uses a real `strands.Agent` against
the live **Google Gemini** API and proves the complete loop:

    Strands Agent → Gemini → tool call → tool result → final structured response

The tool returns a value the model cannot possibly know or guess. If that value
appears in the final structured output, the tool genuinely executed and its
result genuinely reached the model — which is the only way to distinguish real
tool use from a model producing plausible text.

**It never passes without a Gemini API key.** With `GEMINI_API_KEY` set it runs
for real; without it, it skips naming the exact blocker, so a green suite can
never be mistaken for a working model integration.

    uv run pytest tests/integration/test_strands_roundtrip.py -v
"""

from __future__ import annotations

import secrets
import uuid

import pytest
from pydantic import BaseModel, Field
from strands import Agent, tool

from backend.models import RunState, StateTransition
from backend.models.enums import AgentRole
from backend.models.schemas import TransitionEvidence
from backend.models.session import session_scope
from backend.orchestration.state_machine import transition
from backend.shared.config import GeminiConfig, Settings
from backend.shared.model_provider import GeminiModelProvider, build_model_provider

pytestmark = pytest.mark.requires_gemini


def _gemini_or_skip() -> GeminiConfig:
    """Resolve Gemini configuration, or skip naming exactly what is missing."""
    settings = Settings()
    gemini = settings.gemini
    if not isinstance(gemini, GeminiConfig):
        pytest.skip(
            "SKIPPED, NOT PASSED — Google Gemini is not configured: "
            f"{gemini.reason}. Strands agent execution is unproven until this "
            "test runs. Set GEMINI_API_KEY in .env."
        )
    return gemini


# A value the model cannot know, generated fresh each run so it cannot be
# memorised, cached, or coincidentally produced.
_NONCE = secrets.token_hex(4).upper()

_tool_calls: list[str] = []


@tool
def lookup_provider_build_code(provider_id: str) -> str:
    """Look up the internal build code for a provider.

    Args:
        provider_id: The provider to look up.

    Returns:
        The provider's internal build code.
    """
    _tool_calls.append(provider_id)
    return f"BUILD-{_NONCE}"


class ProviderReport(BaseModel):
    """The structured contract the model must satisfy."""

    provider_id: str = Field(description="The provider that was looked up")
    build_code: str = Field(description="The exact build code returned by the tool")


async def test_strands_drives_gemini_through_a_real_tool_call(database: None) -> None:
    """The full loop, with the tool result proving itself.

    `build_code` cannot appear in the output unless the tool actually ran and
    its return value actually reached the model.
    """
    _gemini_or_skip()
    _tool_calls.clear()

    provider = build_model_provider(Settings())
    assert isinstance(provider, GeminiModelProvider), (
        "Gemini must be the active provider when GEMINI_API_KEY is set"
    )

    agent = Agent(
        model=provider.build_model(AgentRole.CHANGE_SCOUT),
        system_prompt=(
            "You look up provider build codes. Always use the "
            "lookup_provider_build_code tool to obtain a build code — you do "
            "not know them and must never guess one."
        ),
        tools=[lookup_provider_build_code],
    )

    result = await agent.invoke_async(
        "What is the internal build code for the provider 'acmepay'? "
        "Use the tool, then report the provider id and the exact build code.",
        structured_output_model=ProviderReport,
    )

    report = result.structured_output

    # 1. Strands returned validated structure, not prose.
    assert isinstance(report, ProviderReport)

    # 2. The tool was genuinely invoked by the model.
    assert _tool_calls, "the model never called the tool"
    assert "acmepay" in _tool_calls[0].lower()

    # 3. The tool's result reached the model and came back out. This is the
    #    assertion that cannot be satisfied by a plausible-sounding answer.
    assert report.build_code == f"BUILD-{_NONCE}", (
        f"expected the tool's value BUILD-{_NONCE}, got {report.build_code!r}"
    )


async def test_validated_output_drives_a_real_state_transition(database: None) -> None:
    """Structured output → deterministic transition, the pattern every agent uses."""
    _gemini_or_skip()

    provider = build_model_provider(Settings())

    class Classification(BaseModel):
        breaking: bool = Field(description="Whether this change can break callers")
        resource: str = Field(description="The endpoint, field, or event affected")

    agent = Agent(
        model=provider.build_model(AgentRole.CHANGE_SCOUT),
        system_prompt=(
            "You classify one provider changelog entry. Decide whether it is "
            "breaking for existing callers and name the affected resource."
        ),
    )

    result = await agent.invoke_async(
        "Changelog entry: The webhook event `payment.paid` has been renamed to "
        "`payment.succeeded`. The old name is no longer emitted.",
        structured_output_model=Classification,
    )
    classification = result.structured_output

    assert isinstance(classification, Classification)
    assert classification.breaking is True
    assert "payment" in classification.resource.lower()

    async with session_scope() as session:
        record = await transition(
            session,
            from_state=RunState.CHANGE_ANALYSIS_RUNNING,
            to_state=RunState.CHANGE_ANALYSIS_COMPLETE,
            evidence=TransitionEvidence(
                reason=f"Classified {classification.resource} as breaking",
                actor=AgentRole.CHANGE_SCOUT.value,
                detail={"breaking": classification.breaking},
            ),
        )
        record_id = record.id

    async with session_scope() as session:
        stored = await session.get(StateTransition, record_id)

    assert stored is not None
    assert stored.to_state is RunState.CHANGE_ANALYSIS_COMPLETE
    assert stored.actor == AgentRole.CHANGE_SCOUT.value


async def test_the_provider_reports_the_configured_model(database: None) -> None:
    """Guards against silently running on an unexpected model."""
    gemini = _gemini_or_skip()

    provider = build_model_provider(Settings())

    assert provider.model_id == gemini.model
    assert uuid.UUID(int=0)  # sanity: the body executed
