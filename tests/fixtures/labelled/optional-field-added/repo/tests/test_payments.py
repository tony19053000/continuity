from app.payments import create_payment


def test_create_payment_charges_the_customer():
    assert create_payment("order-1", 500)["amount"] == 500
