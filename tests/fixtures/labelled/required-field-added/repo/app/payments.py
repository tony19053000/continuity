"""Payment integration for the storefront."""

import httpx
from acmepay import AcmePayClient

client = AcmePayClient(api_version="v1")


def create_payment(order_id: str, amount_cents: int) -> dict:
    """Charge a customer for an order. Used by the checkout flow."""
    return client.post("/v1/charges", json={"order": order_id, "amount": amount_cents})


def refund_payment(charge_id: str) -> httpx.Response:
    """Refund a charge."""
    return httpx.post(f"/v1/charges/{charge_id}/refund")
