from datetime import timedelta

from app.dealguard.engine import DealGuard, GuardUsage
from app.domain.models import AgentCapability, DecisionStatus, MerchantPolicy, ProposedOffer, utc_now
from app.security.signing import sign_deal, verify_deal_signature
from app.domain.models import AuthorizedDeal, DealStatus
from app.agents.providers import GroqProvider, OllamaProvider


def policy_and_capability():
    policy = MerchantPolicy(authorization_expires_at=utc_now() + timedelta(hours=1))
    capability = AgentCapability(
        allowed_products=[policy.product_id], min_price=policy.min_acceptable_price,
        max_discount=policy.max_discount, max_concession_budget=policy.max_freebie_value,
        expires_at=utc_now() + timedelta(hours=1),
    )
    return policy, capability


def offer(**updates):
    payload = {
        "product_id": "iphone-17-pro", "offered_price": 149_000,
        "included_concession_ids": ["warranty", "express"], "delivery_days": 2,
        "justification": "A high-value protected purchase package.", "negotiation_round": 1,
    }
    payload.update(updates)
    return ProposedOffer(**payload)


def test_one_rupee_attack_is_blocked_by_multiple_deterministic_rules():
    policy, capability = policy_and_capability()
    decision = DealGuard().evaluate(offer(offered_price=1, included_concession_ids=[]), policy, capability)
    codes = {issue.code for issue in decision.issues}
    assert decision.status == DecisionStatus.BLOCKED
    assert {"PRICE_FLOOR_VIOLATION", "MINIMUM_PROFIT_VIOLATION", "FLAGSHIP_PRODUCT_PROTECTION"} <= codes


def test_valid_profitable_value_package_is_approved():
    policy, capability = policy_and_capability()
    decision = DealGuard().evaluate(offer(), policy, capability)
    assert decision.status == DecisionStatus.APPROVED
    assert decision.approved is True
    assert decision.economics.concession_cost == 1_900
    assert decision.economics.customer_value == 6_500
    assert decision.economics.gross_profit == 12_100


def test_unknown_concession_never_receives_merchant_cost_or_authority():
    policy, capability = policy_and_capability()
    decision = DealGuard().evaluate(offer(included_concession_ids=["private-admin-voucher"]), policy, capability)
    assert decision.status == DecisionStatus.BLOCKED
    assert decision.decision_code == "UNAUTHORIZED_CONCESSION"


def test_daily_budget_and_transaction_limits_block_after_authority_is_used():
    policy, capability = policy_and_capability()
    decision = DealGuard().evaluate(offer(), policy, capability, GuardUsage(daily_concession_cost=29_000))
    assert decision.status == DecisionStatus.BLOCKED
    assert decision.decision_code == "DAILY_CONCESSION_BUDGET"


def test_human_review_band_is_not_auto_authorized():
    policy, capability = policy_and_capability()
    decision = DealGuard().evaluate(offer(offered_price=147_500, included_concession_ids=[]), policy, capability)
    assert decision.status == DecisionStatus.REVIEW_REQUIRED
    assert decision.decision_code == "HUMAN_APPROVAL_REQUIRED"


def test_expired_capability_is_blocked():
    policy, capability = policy_and_capability()
    expired = capability.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    decision = DealGuard().evaluate(offer(), policy, expired)
    assert decision.status == DecisionStatus.BLOCKED
    assert decision.decision_code == "AUTHORIZATION_EXPIRED"


def test_signed_deal_detects_tampered_price():
    policy, capability = policy_and_capability()
    deal = AuthorizedDeal(
        product_id=policy.product_id, product_name=policy.product_name, final_price=149_000,
        concession_ids=["warranty"], merchant_id=policy.merchant_id, buyer_id="buyer_demo",
        policy_version=policy.policy_version, capability_id=capability.capability_id,
        expires_at=utc_now() + timedelta(minutes=15), status=DealStatus.AUTHORIZED,
    )
    signed = deal.model_copy(update={"signature": sign_deal(deal, "test-secret")})
    assert verify_deal_signature(signed, "test-secret")
    assert not verify_deal_signature(signed.model_copy(update={"final_price": 1}), "test-secret")


def test_llm_provider_adapters_have_explicit_operator_visible_names():
    assert GroqProvider.name == "Groq (hosted LLM)"
    assert OllamaProvider.name == "Ollama (local LLM)"
