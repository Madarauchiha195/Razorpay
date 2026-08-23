from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import BuyerIntent, ProposedOffer


@dataclass(frozen=True)
class BuyerAssessment:
    should_continue: bool
    message: str


class BuyerAgent:
    """Buyer-side reasoning deliberately receives no merchant policy or economics."""

    def assess(self, intent: BuyerIntent, offer: ProposedOffer, customer_value: int) -> BuyerAssessment:
        effective_value = max(offer.offered_price - customer_value, 0)
        if effective_value <= intent.max_budget:
            return BuyerAssessment(True, "The value package meets the buyer's budget goal.")
        if offer.negotiation_round >= 3:
            return BuyerAssessment(False, "The buyer could not reach a suitable value package in time.")
        return BuyerAssessment(True, "The buyer asks the merchant agent to optimize package value.")
