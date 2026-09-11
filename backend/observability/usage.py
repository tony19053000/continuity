"""What a pass actually cost, measured rather than estimated.

`02_ARCHITECTURE.md` §18 asks for total execution time, tool calls, and token
usage. Tool calls already have a home — `tool_invocations` rows, written by the
dispatcher. The other two had none: nothing recorded how long a model call took
or how many tokens it used, so the Cost section of an evaluation report could
only have been guessed at.

This is the collector. It is **opt-in and context-scoped**: with no collector
installed, `record_model_call` does nothing and the production path is
unchanged. The evaluation harness installs one per case, so its cost figures
are counted rather than inferred.

Token counts come from the SDK when it reports them and are `None` when it does
not. `None` is carried through to the report as *unavailable*, never summed as
zero — a cost of zero and a cost nobody measured are different claims.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One structured call to a model."""

    role: str
    duration_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def summary(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(slots=True)
class UsageLog:
    """Every model call made while this collector was installed."""

    calls: list[ModelCall] = field(default_factory=list)

    @property
    def model_calls(self) -> int:
        return len(self.calls)

    @property
    def duration_ms(self) -> int:
        return sum(call.duration_ms for call in self.calls)

    @property
    def tokens(self) -> int | None:
        """Total tokens, or `None` when no call reported any.

        Partial data is summed over the calls that reported it and the count of
        those is in `calls_with_token_counts`, so a partial figure is never
        presented as a total.
        """
        measured = [call.tokens for call in self.calls if call.tokens is not None]
        return sum(measured) if measured else None

    @property
    def calls_with_token_counts(self) -> int:
        return sum(1 for call in self.calls if call.tokens is not None)

    def summary(self) -> dict[str, Any]:
        return {
            "model_calls": self.model_calls,
            "model_duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "calls_with_token_counts": self.calls_with_token_counts,
            "calls": [call.summary() for call in self.calls],
        }


_collector: ContextVar[UsageLog | None] = ContextVar(
    "continuity_usage_collector", default=None
)


@contextmanager
def collecting() -> Iterator[UsageLog]:
    """Collect model-call usage for the duration of this block."""
    log = UsageLog()
    token = _collector.set(log)
    try:
        yield log
    finally:
        _collector.reset(token)


def record_model_call(
    *,
    role: str,
    duration_ms: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Record one model call, if anyone is listening."""
    log = _collector.get()
    if log is None:
        return
    log.calls.append(
        ModelCall(
            role=role,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )


def usage_from(result: Any) -> tuple[int | None, int | None]:
    """Pull input and output token counts out of an SDK result.

    Defensive because it is reading another library's optional shape: Strands
    exposes `result.metrics.accumulated_usage`, the keys have changed across
    versions, and a missing count must come back as `None` rather than 0.
    """
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None)
    if usage is None:
        return None, None

    def _count(*names: str) -> int | None:
        for name in names:
            value = (
                usage.get(name)
                if isinstance(usage, dict)
                else getattr(usage, name, None)
            )
            if isinstance(value, int):
                return value
        return None

    return _count("inputTokens", "input_tokens"), _count(
        "outputTokens", "output_tokens"
    )
