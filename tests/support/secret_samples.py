"""Synthetic credential-shaped strings for testing the secret filter.

These are worthless to an attacker but indistinguishable from real credentials
to a scanner. Written as literals they would be caught by GitHub's push
protection — and "allow the secret" is the wrong reflex in a project whose
entire subject is not leaking credentials. Worse, committing scanner-tripping
strings trains everyone to ignore scan results, which is how a real leak gets
missed.

So every sample is assembled from parts at import time. The complete token
exists in memory during the test, which is the only place the redaction
patterns need to see it; no credential-shaped literal is ever committed.

Import from here rather than pasting a token into a new test.
"""

from __future__ import annotations

from typing import Final

_ALPHA: Final = "abcdefghijklmnopqrstuvwx"
_DIGITS: Final = "1234567890"


def _token(prefix: str, body: str) -> str:
    return prefix + body


# A GitHub token shape, used wherever a test needs "some credential".
GITHUB_TOKEN: Final = _token("ghp" + "_", _DIGITS + _ALPHA)

AWS_ACCESS_KEY_ID: Final = _token("AKIA", "IOSFODNN7EXAMPLE")
AWS_SESSION_KEY: Final = _token("ASIA", "IOSFODNN7EXAMPLE")
GITHUB_OAUTH_TOKEN: Final = _token("gho" + "_", _DIGITS + _ALPHA)
GITHUB_SERVER_TOKEN: Final = _token("ghs" + "_", _DIGITS + _ALPHA)
GITHUB_PAT: Final = _token("github" + "_pat_", "11ABCDEFG0" + _ALPHA + "_" + _DIGITS)
SLACK_TOKEN: Final = _token("xoxb" + "-", f"{_DIGITS}12-{_DIGITS}123-{_ALPHA}")
STRIPE_LIVE_KEY: Final = _token("sk" + "_live_", _ALPHA)
JWT: Final = _token(
    "eyJ",
    "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQabcdefghij",
)

PRIVATE_KEY_BLOCK: Final = (
    "-----BEGIN" + " RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA" + _ALPHA + "\n" + _ALPHA + _DIGITS + "\n"
    "-----END" + " RSA PRIVATE KEY-----"
)

# (name, value) pairs covering every pattern in backend/shared/redaction.py.
ALL_SAMPLES: Final = [
    ("aws_access_key_id", AWS_ACCESS_KEY_ID),
    ("aws_session_key", AWS_SESSION_KEY),
    ("github_classic", GITHUB_TOKEN),
    ("github_oauth", GITHUB_OAUTH_TOKEN),
    ("github_server", GITHUB_SERVER_TOKEN),
    ("github_pat", GITHUB_PAT),
    ("slack", SLACK_TOKEN),
    ("stripe_live", STRIPE_LIVE_KEY),
    ("jwt", JWT),
]
