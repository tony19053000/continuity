"""Secret redaction patterns (`03_SECURITY_ACCESS.md` §2).

Both directions matter. A missed secret is a leak; an over-eager pattern that
redacts ordinary code makes every log useless and pushes people to disable
redaction, which is worse than a narrower pattern.
"""

from __future__ import annotations

import pytest

from backend.shared.redaction import (
    REDACTED,
    contains_secret,
    detected_secret_kinds,
    redact,
)
from tests.support.secret_samples import (
    ALL_SAMPLES,
    AWS_ACCESS_KEY_ID,
    GITHUB_TOKEN,
    PRIVATE_KEY_BLOCK,
)

SECRETS = ALL_SAMPLES


@pytest.mark.parametrize(("name", "secret"), SECRETS, ids=[n for n, _ in SECRETS])
def test_known_secret_shapes_are_redacted(name: str, secret: str) -> None:
    text = f"the credential is {secret} and that is all"
    result = redact(text)

    assert secret not in result
    assert REDACTED in result


def test_private_key_block_is_redacted_including_its_body() -> None:
    result = redact(PRIVATE_KEY_BLOCK)

    assert "MIIEowIBAAKCAQEA" not in result
    assert result == REDACTED


def test_connection_string_password_is_redacted_but_host_survives() -> None:
    result = redact("postgresql://appuser:hunter2isnotgreat@db.internal:5432/continuity")

    assert "hunter2isnotgreat" not in result
    # The host is exactly what makes the log useful; keep it.
    assert "db.internal" in result
    assert "appuser" in result


def test_generic_assignment_keeps_the_key_and_drops_the_value() -> None:
    result = redact('api_key = "abcdefghijklmnopqrstuvwxyz"')

    assert "abcdefghijklmnopqrstuvwxyz" not in result
    # Knowing *which* setting was involved is the point of keeping the prefix.
    assert "api_key" in result


@pytest.mark.parametrize(
    "harmless",
    [
        "def create_payment(amount: int) -> Payment: ...",
        "debug = true",
        "token = ''",
        "response.status_code == 200",
        "from backend.models import User",
        "https://api.example.com/v1/customers",
        "the checkout workflow calls create_payment and renew_subscription",
        "retry_count = 3",
    ],
)
def test_ordinary_code_and_prose_are_untouched(harmless: str) -> None:
    assert redact(harmless) == harmless
    assert not contains_secret(harmless)


def test_multiple_secrets_in_one_string_are_all_redacted() -> None:
    text = f"aws={AWS_ACCESS_KEY_ID} github={GITHUB_TOKEN}"

    result = redact(text)

    assert AWS_ACCESS_KEY_ID not in result
    assert GITHUB_TOKEN not in result


def test_detected_kinds_names_every_match() -> None:
    text = f"{AWS_ACCESS_KEY_ID} and {GITHUB_TOKEN}"

    kinds = detected_secret_kinds(text)

    assert "aws_access_key_id" in kinds
    assert "github_token" in kinds


def test_empty_input_is_handled() -> None:
    assert redact("") == ""
    assert not contains_secret("")
