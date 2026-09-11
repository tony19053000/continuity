"""Structured JSON logging with mandatory redaction.

Redaction is applied by a `logging.Filter` attached to the handler, not by
callers. That placement is deliberate: a caller who forgets to redact still
cannot leak a credential, which is the only arrangement that actually holds
(`03_SECURITY_ACCESS.md` §2, enforcement point 4).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from backend.shared.redaction import redact, redact_deep

# Attributes present on every LogRecord; anything else was supplied by the
# caller via `extra=` and is treated as structured context.
_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class RedactingFilter(logging.Filter):
    """Redacts the formatted message and every structured field.

    Returns True always — this filter never drops a record, it only sanitises
    it. Losing a log line would hide the very incident we want to see.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Resolve %-style args now, so redaction sees the final text rather than
        # a format string whose arguments still hold the secret.
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive; never lose a record
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_ATTRS:
                continue
            record.__dict__[key] = redact_deep(value)

        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value

        if record.exc_info:
            # The traceback stays in the log and never reaches a response body.
            payload["exception"] = redact(self.formatException(record.exc_info))

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler with redaction on the root logger.

    Idempotent: repeated calls replace the handler rather than stacking
    duplicates, which matters because tests and Uvicorn both trigger setup.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn installs its own handlers; route them through ours so access logs
    # are redacted too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
