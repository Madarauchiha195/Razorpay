from __future__ import annotations

from .models import MerchantPolicy, OfferEconomics, ProposedOffer


class EconomicEngine:
    """Recomputes economics from server-owned policy data; offers never supply costs."""

    @staticmethod
    def evaluate(offer: ProposedOffer, policy: MerchantPolicy) -> OfferEconomics:
        concessions = {item.id: item for item in policy.concessions}
        selected = [concessions[item] for item in offer.included_concession_ids if item in concessions]
        concession_cost = sum(item.merchant_cost for item in selected)
        customer_value = sum(item.customer_perceived_value for item in selected)
        payment_cost = (offer.offered_price * policy.payment_fee_bps) // 10_000 + policy.payment_fixed_cost
        gross_profit = offer.offered_price - policy.base_cost - concession_cost - payment_cost
        margin_bps = (gross_profit * 10_000) // offer.offered_price if offer.offered_price else 0
        return OfferEconomics(
            concession_cost=concession_cost,
            customer_value=customer_value,
            payment_cost=payment_cost,
            gross_profit=gross_profit,
            profit_margin_bps=margin_bps,
            direct_discount=max(policy.target_price - offer.offered_price, 0),
        )

    @staticmethod
    def ranked_concessions(policy: MerchantPolicy, desired_ids: list[str]) -> list[str]:
        desired = set(desired_ids)
        items = [item for item in policy.concessions if item.allowed and item.inventory_available]
        items.sort(
            key=lambda item: (
                item.id not in desired,
                -(item.customer_perceived_value / max(item.merchant_cost, 1)),
            )
        )
        return [item.id for item in items]
