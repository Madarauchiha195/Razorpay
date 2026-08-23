from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.economics import EconomicEngine
from ..domain.models import (
    AgentCapability,
    DealDecision,
    DecisionStatus,
    MerchantPolicy,
    PolicyIssue,
    ProposedOffer,
    Severity,
)


@dataclass(frozen=True)
class GuardUsage:
    daily_concession_cost: int = 0
    transactions_today: int = 0
    agent_frozen: bool = False


class DealGuard:
    """A deterministic financial authorization boundary. It has no LLM dependency."""

    def evaluate(
        self,
        offer: ProposedOffer,
        policy: MerchantPolicy,
        capability: AgentCapability,
        usage: GuardUsage = GuardUsage(),
    ) -> DealDecision:
        now = datetime.now(timezone.utc)
        issues: list[PolicyIssue] = []
        economics = EconomicEngine.evaluate(offer, policy)
        items = {item.id: item for item in policy.concessions}

        def block(code: str, message: str, severity: Severity = Severity.CRITICAL) -> None:
            issues.append(PolicyIssue(code=code, severity=severity, message=message))

        if usage.agent_frozen:
            block("AGENT_FROZEN", "The negotiating capability is temporarily frozen.")
        if offer.product_id != policy.product_id or offer.product_id not in capability.allowed_products:
            block("PRODUCT_RESTRICTION", "This product is not authorized for this capability.")
        if now >= policy.authorization_expires_at or now >= capability.expires_at:
            block("AUTHORIZATION_EXPIRED", "The negotiating capability has expired.")
        if offer.negotiation_round > policy.max_negotiation_rounds:
            block("MAX_NEGOTIATION_ROUNDS", "The maximum number of rounds was exceeded.")
        if usage.transactions_today >= min(policy.max_transactions, capability.max_transactions):
            block("TRANSACTION_LIMIT", "The authorized transaction limit has been reached.")
        if offer.offered_price < policy.min_acceptable_price or offer.offered_price < capability.min_price:
            block("PRICE_FLOOR_VIOLATION", "The proposed price is outside the authorized range.")
        if economics.direct_discount > min(policy.max_discount, capability.max_discount):
            block("MAX_DISCOUNT_VIOLATION", "The proposed discount exceeds the delegated limit.")
        if economics.gross_profit < policy.min_profit:
            block("MINIMUM_PROFIT_VIOLATION", "The offer does not preserve the required merchant profit.")
        if economics.concession_cost > min(policy.max_freebie_value, capability.max_concession_budget):
            block("CONCESSION_BUDGET_VIOLATION", "The concession budget exceeds delegated authority.")
        if usage.daily_concession_cost + economics.concession_cost > policy.max_daily_concession_budget:
            block("DAILY_CONCESSION_BUDGET", "The daily concession budget would be exceeded.")

        unknown = [item_id for item_id in offer.included_concession_ids if item_id not in items]
        unavailable = [item_id for item_id in offer.included_concession_ids if item_id in items and (not items[item_id].allowed or not items[item_id].inventory_available)]
        if unknown:
            block("UNAUTHORIZED_CONCESSION", "The offer includes a concession that is not merchant-authorized.")
        if unavailable:
            block("INVENTORY_UNAVAILABLE", "A selected concession is unavailable.")
        if len(offer.included_concession_ids) != len(set(offer.included_concession_ids)):
            block("DUPLICATE_CONCESSION", "The same concession may not be claimed twice.")
        if offer.delivery_days > 14:
            block("DELIVERY_CONSTRAINT", "Delivery duration is outside the supported range.")

        if policy.flagship_product and offer.offered_price < policy.min_acceptable_price:
            block("FLAGSHIP_PRODUCT_PROTECTION", "Flagship protections rejected this offer.")

        if issues:
            risk = min(100, 35 + 15 * len(issues) + (30 if any(x.severity == Severity.CRITICAL for x in issues) else 0))
            return DealDecision(
                approved=False,
                status=DecisionStatus.BLOCKED,
                decision_code=issues[0].code,
                reason=issues[0].message,
                risk_score=risk,
                policy_version=policy.policy_version,
                issues=issues,
                economics=economics,
            )

        if offer.offered_price <= policy.human_approval_threshold:
            warning = PolicyIssue(
                code="HUMAN_APPROVAL_REQUIRED",
                severity=Severity.WARNING,
                message="This offer is valid but requires merchant approval before payment.",
            )
            return DealDecision(
                approved=False,
                status=DecisionStatus.REVIEW_REQUIRED,
                decision_code=warning.code,
                reason=warning.message,
                risk_score=25,
                policy_version=policy.policy_version,
                issues=[warning],
                economics=economics,
            )

        return DealDecision(
            approved=True,
            status=DecisionStatus.APPROVED,
            decision_code="DEAL_APPROVED",
            reason="DealGuard authorized the offer within delegated limits.",
            risk_score=5,
            policy_version=policy.policy_version,
            economics=economics,
        )
