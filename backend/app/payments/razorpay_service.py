from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from ..config import settings
from ..domain.models import AuthorizedDeal


@dataclass(frozen=True)
class CreatedOrder:
    order_id: str
    is_live_checkout: bool
    key_id: str | None


class RazorpayService:
    """Only invoked after an AuthorizedDeal's signature and status are verified."""

    def create_order(self, deal: AuthorizedDeal) -> CreatedOrder:
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            return CreatedOrder(order_id=f"order_mock_{deal.deal_id[-12:]}", is_live_checkout=False, key_id=None)
        try:
            import razorpay

            client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
            order = client.order.create({
                "amount": deal.final_price * 100,
                "currency": "INR",
                "receipt": deal.deal_id[:40],
                "notes": {"deal_id": deal.deal_id, "authorization_id": deal.authorization_id, "platform": "DealMesh"},
            })
            return CreatedOrder(order_id=order["id"], is_live_checkout=True, key_id=settings.razorpay_key_id)
        except Exception as exc:
            # Do not leak provider detail to the client. Operator logs should capture the upstream error.
            raise RuntimeError("Razorpay test order creation failed") from exc

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        if not settings.razorpay_key_secret:
            return False
        payload = f"{order_id}|{payment_id}".encode("utf-8")
        expected = hmac.new(settings.razorpay_key_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        if not settings.razorpay_webhook_secret or not signature:
            return False
        expected = hmac.new(settings.razorpay_webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
