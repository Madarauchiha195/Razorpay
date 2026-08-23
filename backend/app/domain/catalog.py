"""The seeded merchant catalog.

Every product is generated from the same arithmetic so that the delegated-authority invariants
DealGuard enforces hold by construction rather than by hand-tuning. Prices, floors, and
concession costs are all derived from a product's list price in basis points, which is what lets
the catalog span a phone at 19k and a flagship at 150k without any product accidentally becoming
un-negotiable.

The invariants, all verified by ``validate_catalog``:

* ``min_acceptable_price = target_price - max_discount`` - the discount cap and the hard floor can
  never disagree, so an offer at the floor cannot trip MAX_DISCOUNT_VIOLATION.
* ``base_cost`` is solved backwards from the floor, so the cheapest auto-approvable price still
  earns ``min_profit`` even while carrying the entire concession budget. Without this the guard
  blocks nearly every proposal with MINIMUM_PROFIT_VIOLATION.
* ``max_freebie_value`` covers the three cheapest concessions but not all four, so a package has
  room to vary between rounds while the budget rule still binds on a greedy one.
* The authorization lease is long. It is an operational horizon, not a pricing decision, and a
  short one silently fails every negotiation with AUTHORIZATION_EXPIRED once it lapses.
"""

from __future__ import annotations

from datetime import timedelta

from .models import Concession, MerchantPolicy, utc_now


# Policy shape, in basis points of the product's list price. One set of ratios for every product
# keeps the guard, the policy editor, and the economic engine behaving identically across the
# catalog; only the list price and the concession mix actually differ.
_MAX_DISCOUNT_BPS = 600
_MIN_PROFIT_BPS = 700
_FREEBIE_BUDGET_BPS = 180
# The slice of the discount range that requires a human. It sits directly above the hard floor,
# so the cheapest prices land in review and the rest can be authorized autonomously.
_REVIEW_BAND_SHARE = 0.25

# The daily concession budget is a circuit breaker on the negotiating agent, and the counter behind
# it is a single merchant-wide total. So the cap has to be merchant-wide too: scaling it per product
# means spending on an expensive product silently exhausts a cheap product's smaller cap, which
# blocks a perfectly legal offer and - because every block increments the violation counter - can
# freeze the agent outright. One shared number for one shared counter.
CATALOG_DAILY_CONCESSION_BUDGET = 60_000

CATALOG_AUTHORIZATION_DAYS = 365

# Present on every product, so a shopper's value preferences survive switching between products.
_SHARED_CONCESSIONS = (
    ("warranty", "Extended Warranty", 80, 270),
    ("express", "Express Delivery", 45, 170),
)


def _round_to(value: float, step: int) -> int:
    """Round to a tidy multiple, never below one step, so no derived amount collapses to zero."""
    return max(step, int(round(value / step)) * step)


def _bps(target_price: int, bps: int, step: int) -> int:
    return _round_to(target_price * bps / 10_000, step)


def _build(
    product_id: str,
    product_name: str,
    target_price: int,
    flagship: bool,
    extras: tuple[tuple[str, str, int, int], ...],
) -> MerchantPolicy:
    concessions = [
        Concession(
            id=concession_id,
            name=name,
            merchant_cost=_bps(target_price, cost_bps, 10),
            customer_perceived_value=_bps(target_price, value_bps, 10),
        )
        for concession_id, name, cost_bps, value_bps in (*_SHARED_CONCESSIONS, *extras)
    ]

    max_discount = _bps(target_price, _MAX_DISCOUNT_BPS, 100)
    min_profit = _bps(target_price, _MIN_PROFIT_BPS, 100)
    freebie_budget = _bps(target_price, _FREEBIE_BUDGET_BPS, 10)
    # A single concession must always fit, or even a one-item package is blocked outright.
    freebie_budget = max(freebie_budget, max(item.merchant_cost for item in concessions))

    min_acceptable = target_price - max_discount
    review_band = _round_to(max_discount * _REVIEW_BAND_SHARE, 100)

    return MerchantPolicy(
        product_id=product_id,
        product_name=product_name,
        # Solved from the floor so that the floor price, carrying a full concession budget, still
        # clears min_profit. Every derived figure above feeds this one number.
        base_cost=min_acceptable - freebie_budget - min_profit,
        target_price=target_price,
        min_acceptable_price=min_acceptable,
        min_profit=min_profit,
        max_discount=max_discount,
        max_freebie_value=freebie_budget,
        max_daily_concession_budget=CATALOG_DAILY_CONCESSION_BUDGET,
        flagship_product=flagship,
        human_approval_threshold=min_acceptable + review_band,
        authorization_expires_at=utc_now() + timedelta(days=CATALOG_AUTHORIZATION_DAYS),
        concessions=concessions,
    )


def default_catalog() -> list[MerchantPolicy]:
    """The negotiable catalog, in the order the studio lists it."""
    return [
        _build("iphone-17-pro", "iPhone 17 Pro", 150_000, True, (
            ("case", "Premium Phone Case", 40, 130),
            ("voucher", "Future Purchase Voucher", 27, 100),
        )),
        _build("galaxy-s26-ultra", "Galaxy S26 Ultra", 134_999, True, (
            ("spen", "S Pen Pro Bundle", 60, 210),
            ("cloud", "2TB Cloud Storage (1 Year)", 33, 160),
        )),
        _build("macbook-air-m5", "MacBook Air M5", 119_900, True, (
            ("sleeve", "Leather Sleeve", 50, 165),
            ("care", "Accidental Damage Cover", 70, 240),
        )),
        _build("pixel-10-pro", "Pixel 10 Pro", 89_999, False, (
            ("buds", "Pixel Buds Pro 2", 65, 230),
            ("photos", "Google One Premium (1 Year)", 40, 170),
        )),
        _build("ipad-air-m3", "iPad Air M3", 64_900, False, (
            ("pencil", "Stylus Pencil Pro", 70, 250),
            ("keyboard", "Folio Keyboard", 60, 200),
        )),
        _build("galaxy-buds-4-pro", "Galaxy Buds 4 Pro", 18_999, False, (
            ("tips", "Comfort Ear Tip Kit", 55, 190),
            ("podcase", "Charging Case Cover", 45, 150),
        )),
    ]


SEED_CATALOG: tuple[MerchantPolicy, ...] = tuple(default_catalog())
DEFAULT_PRODUCT_ID: str = SEED_CATALOG[0].product_id


def validate_catalog(catalog: tuple[MerchantPolicy, ...] | list[MerchantPolicy] = SEED_CATALOG) -> list[str]:
    """Report any product whose policy contradicts itself.

    A contradictory policy does not raise - it silently blocks every negotiation for that product
    with a guard code that looks like a working safety rule. This turns that class of bug into a
    test failure instead of a demo that mysteriously never authorizes anything.
    """
    problems: list[str] = []
    for policy in catalog:
        label = policy.product_id
        floor = policy.human_approval_threshold + 1
        costs = sorted(item.merchant_cost for item in policy.concessions)

        if policy.min_acceptable_price != policy.target_price - policy.max_discount:
            problems.append(f"{label}: price floor and discount cap disagree")
        if floor > policy.target_price:
            problems.append(f"{label}: no auto-approvable price exists below the list price")
        # Worst case: the cheapest approvable price carrying the entire delegated freebie budget.
        worst_case_profit = floor - policy.base_cost - policy.max_freebie_value
        if worst_case_profit < policy.min_profit:
            problems.append(f"{label}: floor price cannot earn min_profit ({worst_case_profit} < {policy.min_profit})")
        if policy.target_price - floor > policy.max_discount:
            problems.append(f"{label}: the approvable band reaches past the discount cap")
        if costs and costs[0] > policy.max_freebie_value:
            problems.append(f"{label}: even the cheapest concession exceeds the freebie budget")
        if len(costs) >= 3 and sum(costs[:3]) > policy.max_freebie_value:
            problems.append(f"{label}: freebie budget cannot fund a three-item package")
        if policy.max_daily_concession_budget < policy.max_freebie_value:
            problems.append(f"{label}: daily concession budget is smaller than a single package")
        if len({item.id for item in policy.concessions}) != len(policy.concessions):
            problems.append(f"{label}: duplicate concession id")

    if len({policy.product_id for policy in catalog}) != len(catalog):
        problems.append("catalog contains duplicate product ids")
    # One shared counter needs one shared cap. If products disagree, spending on the product with
    # the larger cap silently exhausts the smaller one, blocking a legal offer on a product that
    # has not been negotiated at all.
    if len({policy.max_daily_concession_budget for policy in catalog}) > 1:
        problems.append("products disagree on max_daily_concession_budget, which is a shared counter")
    return problems
