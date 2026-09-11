"""A small, real project for the product-loop tests.

Shaped like the kind of repository Continuity is for — a manifest declaring a
provider SDK, a client calling its endpoints, and tests that really run — and
deliberately self-contained: the `acmepay` module ships inside the fixture, so
`pytest` inside the migration workspace actually passes rather than failing on
a missing import.

That matters. A product test whose validation step cannot execute would prove
the pipeline moves between states, not that it produces working code.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: The provider SDK, vendored into the fixture so its tests can import it.
SDK = '''\
"""A stand-in for a provider SDK, so the fixture's tests can run."""


class AcmePayClient:
    def __init__(self, api_version: str = "v1") -> None:
        self.api_version = api_version

    def post(self, path: str, json: dict) -> dict:
        missing = [field for field in REQUIRED_FIELDS if field not in json]
        if missing:
            raise ValueError(f"missing required field(s): {missing}")
        return {"path": path, **json}


REQUIRED_FIELDS = ["amount"]
'''

CLIENT = '''\
"""Payment integration for the commerce API."""

import httpx
from acmepay import AcmePayClient

client = AcmePayClient(api_version="v1")


def create_payment(order_id: str, amount_cents: int) -> dict:
    """Charge a customer for an order. Used by the checkout flow."""
    return client.post("/v1/charges", json={"order": order_id, "amount": amount_cents})


def refund_payment(charge_id: str) -> httpx.Response:
    """Refund a charge. Present so the file has a real HTTP client, which is
    what the extractor looks for when deciding a call targets a provider."""
    return httpx.post(f"/v1/charges/{charge_id}/refund")
'''

TESTS = '''\
from app.payments import create_payment


def test_create_payment_charges_the_customer():
    assert create_payment("order-1", 500)["amount"] == 500


def test_create_payment_names_the_order():
    assert create_payment("order-2", 100)["order"] == "order-2"
'''

MANIFEST = '''\
[project]
name = "commerce-api"
version = "1.0.0"
dependencies = ["httpx>=0.28", "acmepay>=2.1"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]
'''


def write_product_repo(root: Path) -> Path:
    """Lay the project down and make it a real git checkout."""
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    (root / "pyproject.toml").write_text(MANIFEST)
    (root / "acmepay.py").write_text(SDK)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "payments.py").write_text(CLIENT)
    (root / "tests" / "test_payments.py").write_text(TESTS)

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607 - fixture setup, resolved from PATH
            cwd=root,
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )

    git("init", "-b", "main")
    git("config", "user.email", "dev@example.test")
    git("config", "user.name", "Dev")
    git("add", "-A")
    git("commit", "-m", "initial")
    return root
