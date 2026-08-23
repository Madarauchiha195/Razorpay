from __future__ import annotations

import random
import re
from dataclasses import dataclass

from ..domain.economics import EconomicEngine
from ..domain.models import BuyerIntent, MerchantPolicy, ProposedOffer
from .providers import LLMProvider, configured_provider, negotiable_band

DETERMINISTIC_ENGINE = "Deterministic economic engine"

# Varied phrasing so repeated demo runs don't read like a canned script.
_JUSTIFICATIONS = [
    "I can't cut the price further, so I've built a higher-value package with priority benefits.",
    "This is the strongest bundle I can authorise at this price - the extras cost you nothing.",
    "Rather than discount the hardware, I've added the benefits buyers value most.",
    "I've protected the price and loaded the package instead, so you get more for the same spend.",
    "This keeps the deal within what I'm allowed to approve while maximising what you receive.",
]


@dataclass
class Proposal:
    """A merchant proposal plus honest attribution of what produced it."""

    offer: ProposedOffer
    engine: str
    llm_used: bool
    error: str | None = None


class MerchantAgent:
    """Proposes packages. Its output is intentionally non-authoritative."""

    def __init__(self, provider: LLMProvider | None = None, *, offline: bool = False):
        # `provider=None` means "auto-detect from settings", NOT "no provider". Without an
        # explicit opt-out there is no way to ask for the offline engine, so any caller that
        # passed None to mean "offline" silently became a network client the moment a key was
        # configured - which is exactly how a test suite turns into a quota-consuming one.
        self.provider = None if offline else (provider if provider is not None else configured_provider())
        self.last_error: str | None = None
        self.last_engine: str = DETERMINISTIC_ENGINE

    @property
    def mode(self) -> str:
        return self.provider.name if self.provider else DETERMINISTIC_ENGINE

    def propose(self, policy: MerchantPolicy, intent: BuyerIntent, round_number: int, feedback: str = "") -> Proposal:
        """Ask the configured LLM first; fall back to the rules engine, but say so."""
        if self.provider:
            outcome = self.provider.generate(policy, intent, round_number, feedback)
            if outcome.offer is not None:
                self.last_error = None
                self.last_engine = outcome.engine
                # The model may return a different product_id or an out-of-band price; DealGuard
                # will catch it, but pinning the product here avoids cross-product confusion.
                offer = outcome.offer.model_copy(update={"product_id": policy.product_id})
                return Proposal(offer, outcome.engine, True)
            self.last_error = outcome.error
            self.last_engine = DETERMINISTIC_ENGINE
            return Proposal(
                self._deterministic_offer(policy, intent, round_number),
                f"{DETERMINISTIC_ENGINE} (fallback: {outcome.error})",
                False,
                outcome.error,
            )

        self.last_engine = DETERMINISTIC_ENGINE
        return Proposal(self._deterministic_offer(policy, intent, round_number), DETERMINISTIC_ENGINE, False)

    def _deterministic_offer(self, policy: MerchantPolicy, intent: BuyerIntent, round_number: int) -> ProposedOffer:
        request = intent.request_message.lower()
        attack_amount = self._adversarial_price(request)
        # The first unsafe proposal gives the demo a visible proof that policy checks cannot be bypassed.
        if round_number == 1 and attack_amount is not None:
            return ProposedOffer(
                product_id=policy.product_id, offered_price=attack_amount, included_concession_ids=[],
                delivery_days=2, justification="Buyer-requested direct-price proposal.", negotiation_round=round_number,
            )
        ranked = EconomicEngine.ranked_concessions(policy, intent.desired_freebies)
        selected: list[str] = []
        total_cost = 0
        # Vary the bundle size between runs; the budget check below still binds.
        bundle_limit = random.choice([1, 2, 2, 3])
        for concession_id in ranked:
            item = next(item for item in policy.concessions if item.id == concession_id)
            if total_cost + item.merchant_cost <= policy.max_freebie_value:
                selected.append(concession_id)
                total_cost += item.merchant_cost
            if len(selected) >= bundle_limit:
                break

        return ProposedOffer(
            product_id=policy.product_id,
            offered_price=self._varied_price(policy, round_number),
            included_concession_ids=selected,
            delivery_days=min(intent.preferred_delivery_days, 2),
            justification=random.choice(_JUSTIFICATIONS),
            negotiation_round=round_number,
        )

    @staticmethod
    def _varied_price(policy: MerchantPolicy, round_number: int) -> int:
        """A different price each run, always inside the auto-approvable band.

        The band is derived from merchant policy, not guessed: it never goes below the
        discount cap, the hard floor, or the human-review threshold. Later rounds concede
        more, so a multi-round negotiation still trends downward.
        """
        floor, ceiling = negotiable_band(policy)
        floor = max(floor, policy.target_price - policy.max_discount)
        if ceiling <= floor:
            return ceiling
        span = ceiling - floor
        # Round 1 opens in the top half; each further round opens up more of the band.
        opening = max(0, span - int(span * min(1.0, 0.35 * round_number)))
        price = random.randint(floor + opening, ceiling)
        # Land on a tidy number, but scale the rounding to the band. A ₹100 step across an
        # ₹800 band would leave only eight possible prices, which stops looking negotiated.
        step = 100 if span >= 3_000 else 10
        rounded = price - (price % step)
        return rounded if rounded >= floor else price

    @staticmethod
    def _adversarial_price(message: str) -> int | None:
        if not any(token in message for token in ("ignore", "bypass", "for ₹1", "for rs 1", "for 1 rupee", "reveal")):
            return None
        match = re.search(r"(?:₹|rs\.?\s*)(\d{1,7})", message)
        return int(match.group(1)) if match else 1
