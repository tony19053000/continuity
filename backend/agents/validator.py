"""C7-03: the Validator interprets results. It does not produce them.

The agent is given counts that a process already produced and asked what they
mean — which failures look related to the provider change, what the likely cause
is. It never runs anything, and `ValidatorInput` carries parsed integers rather
than a command, so there is no shape in which it could.

Interpretation is optional. When no model is available the deterministic reading
stands on its own: exit code zero and no failures is a pass, and anything else
is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.agents.contracts import ValidatorInput, ValidatorOutput
from backend.agents.specialists import ValidatorAgent
from backend.observability.logging import get_logger
from backend.shared.model_provider import ModelProvider
from backend.validation.results import ParsedTestResult

logger = get_logger(__name__)

#: How much output the interpreter sees. Enough for a traceback, not a suite.
MAX_EXCERPT = 6_000


@dataclass(slots=True)
class Validation:
    """A deterministic verdict, optionally with an interpretation attached."""

    parsed: ParsedTestResult
    interpretation: ValidatorOutput | None = None

    @property
    def passed(self) -> bool:
        """Decided by the numbers, never by the model.

        The agent may believe a failure is unrelated to the provider change and
        be right — but that is a diagnosis, not permission to call a red suite
        green.
        """
        return self.parsed.ok

    def failure_evidence(self) -> dict[str, object]:
        return {
            "exit_code": self.parsed.exit_code,
            "passed": self.parsed.passed,
            "failed": self.parsed.failed,
            "skipped": self.parsed.skipped,
            "timed_out": self.parsed.timed_out,
            "failing_test_ids": list(self.parsed.failing_test_ids),
            "output_excerpt": self.parsed.output_excerpt[-MAX_EXCERPT:],
            "failure_summary": (
                self.interpretation.failure_summary if self.interpretation else None
            ),
            "likely_causes": (
                list(self.interpretation.likely_causes) if self.interpretation else []
            ),
            "related_to_provider_change": (
                self.interpretation.related_to_provider_change
                if self.interpretation
                else None
            ),
        }


async def validate(
    parsed: ParsedTestResult, *, model_provider: ModelProvider | None = None
) -> Validation:
    """Attach an interpretation to a result, if a model is available."""
    validation = Validation(parsed=parsed)

    if model_provider is None or parsed.ok:
        # Nothing to diagnose about a passing suite, and no reason to spend a
        # model call on one.
        return validation

    agent = ValidatorAgent(model_provider)
    try:
        validation.interpretation = await agent.run(
            ValidatorInput(
                suite=parsed.suite,
                command=parsed.command,
                exit_code=parsed.exit_code if parsed.exit_code is not None else -1,
                passed=parsed.passed,
                failed=parsed.failed,
                skipped=parsed.skipped,
                failing_test_ids=list(parsed.failing_test_ids),
                output_excerpt=parsed.output_excerpt[-MAX_EXCERPT:],
            )
        )
    except Exception as exc:
        # Losing the interpretation degrades the diagnosis; losing the counts
        # would lose the facts. The counts already exist.
        logger.warning(
            "continuity.validator_interpretation_failed",
            extra={"error": type(exc).__name__},
        )

    return validation
