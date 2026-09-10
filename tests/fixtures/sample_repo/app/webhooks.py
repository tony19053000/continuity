"""Inbound provider webhooks."""

from fastapi import APIRouter, Request

router = APIRouter()


@router.post("/webhooks/acmepay")
async def handle_acmepay_webhook(request: Request):
    """Handle payment lifecycle events from the provider."""
    verify_signature(request.headers["X-Hub-Signature"], webhook_secret)
    event = await request.json()

    if event["type"] == "payment.paid":
        return mark_order_paid(event["data"]["order"])
    return {"ignored": True}
