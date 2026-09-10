from app.services.payment_service import create_payment
from fastapi import APIRouter

router = APIRouter()


@router.post("/checkout")
async def checkout(order_id: str, amount: int):
    return create_payment(order_id, amount)
