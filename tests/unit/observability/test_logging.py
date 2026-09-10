"""C1-04 acceptance: logging is structured and redacts at the handler level.

The handler-level placement is the property under test. A caller who forgets to
redact must still be unable to leak a credential — otherwise the control only
works when everyone remembers it, which is not a control.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from backend.observability.logging import JsonFormatter, RedactingFilter, configure_logging
from tests.support.secret_samples import GITHUB_TOKEN

SECRET = GITHUB_TOKEN


@pytest.fixture
def captured() -> tuple[logging.Logger, StringIO]:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())

    logger = logging.getLogger("continuity.test.logging")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream


def _records(stream: StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_output_is_one_json_object_per_line(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured

    logger.info("something happened")

    (record,) = _records(stream)
    assert record["level"] == "INFO"
    assert record["message"] == "something happened"
    assert "timestamp" in record


def test_a_secret_in_the_message_is_redacted(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured

    logger.info("calling github with %s", SECRET)

    assert SECRET not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()


def test_a_secret_in_a_structured_field_is_redacted(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured

    logger.info("tool call", extra={"tool": "github_read", "argument": SECRET})

    (record,) = _records(stream)
    assert record["tool"] == "github_read"
    assert SECRET not in json.dumps(record)


def test_a_secret_in_a_traceback_is_redacted(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured

    try:
        raise ValueError(f"failed with token {SECRET}")
    except ValueError:
        logger.exception("operation failed")

    assert SECRET not in stream.getvalue()


def test_a_secret_nested_in_a_dict_extra_is_redacted(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    """`extra={"tool_args": {...}}` is how agent context will actually be logged.

    Redacting only top-level strings would let every credential in a tool
    argument dict through — the exact shape this project logs most often.
    """
    logger, stream = captured

    logger.info("tool call", extra={"tool_args": {"headers": {"Authorization": SECRET}}})

    assert SECRET not in stream.getvalue()


def test_a_secret_inside_a_list_extra_is_redacted(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured

    logger.info("argv", extra={"argv": ["curl", "-H", SECRET]})

    assert SECRET not in stream.getvalue()


def test_a_secret_in_an_object_repr_is_redacted(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    """The formatter serialises unknown objects with `str`, so redact them too."""

    class Config:
        def __repr__(self) -> str:
            return f"Config(token={SECRET})"

    logger, stream = captured

    logger.info("config loaded", extra={"config": Config()})

    assert SECRET not in stream.getvalue()


def test_deeply_nested_secrets_are_redacted(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    logger, stream = captured

    logger.info("deep", extra={"a": {"b": [{"c": {"d": [SECRET]}}]}})

    assert SECRET not in stream.getvalue()


def test_numbers_stay_typed_so_logs_remain_queryable(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    """Redaction must not stringify every value and destroy structure."""
    logger, stream = captured

    logger.info("metrics", extra={"attempt": 2, "passed": True, "ratio": 0.5})

    (record,) = _records(stream)
    assert record["attempt"] == 2
    assert record["passed"] is True
    assert record["ratio"] == 0.5


def test_structured_context_survives_redaction(
    captured: tuple[logging.Logger, StringIO],
) -> None:
    """Redaction must not destroy the fields that make a log useful."""
    logger, stream = captured

    logger.info("run advanced", extra={"run_id": "abc-123", "state": "validation_passed"})

    (record,) = _records(stream)
    assert record["run_id"] == "abc-123"
    assert record["state"] == "validation_passed"


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    first = len(logging.getLogger().handlers)

    configure_logging("INFO")
    second = len(logging.getLogger().handlers)

    assert first == second == 1
