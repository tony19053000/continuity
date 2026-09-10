"""Payment integration for the commerce API."""

import httpx
from acmepay import AcmePayClient

client = AcmePayClient(api_version="v1")


def create_payment(order_id: str, amount_cents: int) -> dict:
    """Charge a customer for an order. Used by the checkout flow."""
    return client.post("/v1/charges", json={"order": order_id, "amount": amount_cents})


def renew_subscription(subscription_id: str) -> dict:
    """Renew a subscription at the end of its billing period."""
    return client.post("/v1/subscriptions/renew", json={"id": subscription_id})


def refund_payment(charge_id: str) -> dict:
    return httpx.post(f"/v1/charges/{charge_id}/refund")
