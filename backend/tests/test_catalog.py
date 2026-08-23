"""Catalog, rotation, and live-proposal-sanitisation tests.

These cover the failure modes that do not raise: a product whose policy contradicts itself, or a
model response that fails strict validation, both degrade into "the demo never authorizes anything"
rather than an error anyone can see.
"""

from datetime import timedelta

import pytest

from app.agents.merchant import DETERMINISTIC_ENGINE, MerchantAgent
from app.agents.providers import affordable_concession_count, coerce_offer, negotiable_band
from app.db import DealMeshRepository
from app.dealguard import DealGuard
from app.domain.catalog import (
    CATALOG_DAILY_CONCESSION_BUDGET,
    DEFAULT_PRODUCT_ID,
    SEED_CATALOG,
    default_catalog,
    validate_catalog,
)
from app.domain.models import AgentCapability, BuyerIntent, DecisionStatus, ProposedOffer, utc_now
from app.services.orchestrator import NegotiationCoordinator


@pytest.fixture
def repository() -> DealMeshRepository:
    repo = DealMeshRepository("sqlite://")
    repo.initialise()
    return repo


def test_seed_catalog_has_no_self_contradicting_policy():
    assert validate_catalog() == []


def test_catalog_exposes_more_than_one_product_and_a_stable_default():
    assert len(SEED_CATALOG) > 1
    assert DEFAULT_PRODUCT_ID == SEED_CATALOG[0].product_id
    assert len({policy.product_id for policy in SEED_CATALOG}) == len(SEED_CATALOG)


@pytest.mark.parametrize("policy", SEED_CATALOG, ids=lambda policy: policy.product_id)
def test_every_product_can_actually_be_authorized(policy):
    """A product nobody can buy is the failure mode a price-band bug produces."""
    floor, ceiling = negotiable_band(policy)
    assert floor <= ceiling, "no auto-approvable price exists"

    affordable = affordable_concession_count(policy)
    assert affordable >= 1, "not even one concession fits the delegated budget"
    cheapest = sorted(policy.concessions, key=lambda item: item.merchant_cost)[:affordable]

    capability = NegotiationCoordinator.capability_for(policy)
    # The hardest legal case: the cheapest approvable price carrying the largest legal package.
    decision = DealGuard().evaluate(
        ProposedOffer(
            product_id=policy.product_id, offered_price=floor,
            included_concession_ids=[item.id for item in cheapest], delivery_days=2,
            justification="Worst-case authorizable package.", negotiation_round=1,
        ),
        policy, capability,
    )
    assert decision.status == DecisionStatus.APPROVED, decision.reason
    assert decision.economics.gross_profit >= policy.min_profit


@pytest.mark.parametrize("policy", SEED_CATALOG, ids=lambda policy: policy.product_id)
def test_price_below_the_floor_is_still_blocked_for_every_product(policy):
    capability = NegotiationCoordinator.capability_for(policy)
    decision = DealGuard().evaluate(
        ProposedOffer(
            product_id=policy.product_id, offered_price=policy.min_acceptable_price - 1,
            included_concession_ids=[], delivery_days=2,
            justification="Below the merchant floor.", negotiation_round=1,
        ),
        policy, capability,
    )
    assert decision.status == DecisionStatus.BLOCKED
    assert "PRICE_FLOOR_VIOLATION" in {issue.code for issue in decision.issues}


def test_daily_concession_budget_is_shared_because_the_counter_is_shared():
    """A per-product cap lets spending on one product block a different, untouched product."""
    caps = {policy.max_daily_concession_budget for policy in SEED_CATALOG}
    assert caps == {CATALOG_DAILY_CONCESSION_BUDGET}
    assert CATALOG_DAILY_CONCESSION_BUDGET >= max(p.max_freebie_value for p in SEED_CATALOG) * 5


def test_authorization_lease_outlives_a_demo():
    """A short lease fails every negotiation with AUTHORIZATION_EXPIRED once it lapses."""
    horizon = utc_now() + timedelta(days=30)
    assert all(policy.authorization_expires_at > horizon for policy in SEED_CATALOG)


def test_policy_lookup_is_per_product_and_unknown_ids_raise(repository):
    for policy in SEED_CATALOG:
        assert repository.get_policy(policy.product_id).product_id == policy.product_id
    # Silently substituting another product's policy would authorize money against wrong economics.
    with pytest.raises(KeyError):
        repository.get_policy("not-a-real-product")
    assert repository.get_policy().product_id == DEFAULT_PRODUCT_ID
    assert len(repository.list_products()) == len(SEED_CATALOG)


def test_public_product_list_hides_merchant_economics(repository):
    confidential = {"base_cost", "min_profit", "min_acceptable_price", "max_discount", "max_freebie_value"}
    for product in repository.list_products():
        assert confidential.isdisjoint(product.keys())
        for concession in product["concessions"]:
            assert "merchant_cost" not in concession


def test_daily_counters_roll_over_instead_of_blocking_forever(repository):
    repository.add_daily_concession_cost(29_000)
    assert repository.get_usage()["daily_concession_cost"] == 29_000
    # Simulate the stored counters belonging to a previous day.
    with repository.sessions.begin() as db:
        from app.db.repository import RiskStateRow
        db.get(RiskStateRow, 1).usage_date = utc_now().date() - timedelta(days=1)
    assert repository.get_usage()["daily_concession_cost"] == 0


def test_offline_agent_never_acquires_a_provider_even_with_a_key_configured():
    """Guards the whole suite against silently becoming a network client.

    `MerchantAgent(provider=None)` means "auto-detect from settings", so once GROQ_API_KEY is
    present it resolves to a live provider. The offline tests below run hundreds of proposals; if
    they auto-detected, every `pytest` run would spend real API quota and take minutes instead of
    seconds. `offline=True` is the explicit opt-out those tests depend on.
    """
    agent = MerchantAgent(offline=True)
    assert agent.provider is None
    assert agent.mode == DETERMINISTIC_ENGINE

    proposal = agent.propose(SEED_CATALOG[0], BuyerIntent(product_id=SEED_CATALOG[0].product_id), 1)
    assert proposal.llm_used is False
    assert proposal.error is None


def test_repeated_proposals_vary_in_price():
    """The offline engine must not return one constant price, or the demo looks like mock data."""
    policy = SEED_CATALOG[0]
    intent = BuyerIntent(product_id=policy.product_id, product_name=policy.product_name)
    agent = MerchantAgent(offline=True)
    prices = {agent.propose(policy, intent, 1).offer.offered_price for _ in range(25)}
    assert len(prices) > 1
    floor, ceiling = negotiable_band(policy)
    assert all(floor <= price <= ceiling for price in prices)


def test_proposal_never_leaves_the_authorizable_band_across_all_rounds():
    policy = SEED_CATALOG[0]
    intent = BuyerIntent(product_id=policy.product_id, product_name=policy.product_name)
    agent = MerchantAgent(offline=True)
    floor, ceiling = negotiable_band(policy)
    for round_number in range(1, policy.max_negotiation_rounds + 1):
        for _ in range(15):
            price = agent.propose(policy, intent, round_number).offer.offered_price
            assert floor <= price <= ceiling


def test_sanctioned_brief_never_reveals_cost_structure():
    """Only the price band is disclosed; the inputs that produced it are not."""
    from app.agents.providers import _public_brief

    policy = SEED_CATALOG[0]
    intent = BuyerIntent(product_id=policy.product_id, product_name=policy.product_name)
    brief = repr(_public_brief(policy, intent, 1, ""))
    for secret in (policy.base_cost, policy.min_profit, policy.min_acceptable_price):
        assert str(secret) not in brief
    for concession in policy.concessions:
        assert f"merchant_cost': {concession.merchant_cost}" not in brief


def test_sloppy_model_json_is_coerced_rather_than_silently_dropped():
    policy = SEED_CATALOG[0]
    floor, ceiling = negotiable_band(policy)
    offer = coerce_offer(
        '{"offer": {"offered_price": "1", "included_concession_ids": ["warranty", "made-up-perk"],'
        ' "delivery_days": 99, "justification": "  ", "chatty_extra_key": true}}',
        policy, 2,
    )
    # A float-or-string price out of band is clamped, not accepted and not thrown away.
    assert offer.offered_price == floor
    # An invented concession id never acquires authority.
    assert offer.included_concession_ids == ["warranty"]
    assert offer.delivery_days == 14
    assert len(offer.justification) >= 3
    assert offer.product_id == policy.product_id
    assert floor <= offer.offered_price <= ceiling


def test_default_catalog_returns_independent_instances():
    """Mutating one caller's copy must not rewrite the shared seed."""
    first, second = default_catalog(), default_catalog()
    first[0].target_price = 1
    assert second[0].target_price != 1
    assert SEED_CATALOG[0].target_price != 1
