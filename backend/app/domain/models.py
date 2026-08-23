from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


Money = Annotated[StrictInt, Field(ge=0, le=10_000_000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DecisionStatus(str, Enum):
    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"


class DealStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    AUTHORIZED = "AUTHORIZED"
    PAYMENT_CREATED = "PAYMENT_CREATED"
    PAID = "PAID"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Concession(StrictModel):
    id: StrictStr = Field(min_length=2, max_length=64)
    name: StrictStr = Field(min_length=2, max_length=80)
    merchant_cost: Money
    customer_perceived_value: Money
    inventory_available: StrictBool = True
    allowed: StrictBool = True


class MerchantPolicy(StrictModel):
    product_id: StrictStr = "iphone-17-pro"
    product_name: StrictStr = "iPhone 17 Pro"
    merchant_id: StrictStr = "merchant_demo"
    base_cost: Money = 135_000
    target_price: Money = 150_000
    min_acceptable_price: Money = 147_000
    min_profit: Money = 10_000
    max_discount: Money = 3_000
    max_freebie_value: Money = 2_000
    max_negotiation_rounds: StrictInt = Field(default=3, ge=1, le=10)
    max_daily_concession_budget: Money = 30_000
    payment_fee_bps: StrictInt = Field(default=0, ge=0, le=2_000)
    payment_fixed_cost: Money = 0
    flagship_product: StrictBool = True
    human_approval_threshold: Money = 147_500
    max_transactions: StrictInt = Field(default=100, ge=1, le=100_000)
    allowed_customer_segments: list[StrictStr] = Field(default_factory=lambda: ["retail", "loyal"])
    policy_version: StrictStr = "v1"
    authorization_expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(days=1))
    concessions: list[Concession] = Field(
        default_factory=lambda: [
            Concession(id="warranty", name="Extended Warranty", merchant_cost=1_200, customer_perceived_value=4_000),
            Concession(id="express", name="Express Delivery", merchant_cost=700, customer_perceived_value=2_500),
            Concession(id="case", name="Premium Phone Case", merchant_cost=600, customer_perceived_value=2_000),
            Concession(id="voucher", name="Future Purchase Voucher", merchant_cost=400, customer_perceived_value=1_500),
        ]
    )


class AgentCapability(StrictModel):
    capability_id: StrictStr = "cap_demo_iphone_v1"
    merchant_id: StrictStr = "merchant_demo"
    allowed_products: list[StrictStr] = Field(default_factory=lambda: ["iphone-17-pro"])
    max_discount: Money = 3_000
    min_price: Money = 147_000
    max_concession_budget: Money = 2_000
    max_transactions: StrictInt = Field(default=100, ge=1)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(days=1))
    policy_version: StrictStr = "v1"


class BuyerIntent(StrictModel):
    product_id: StrictStr = "iphone-17-pro"
    product_name: StrictStr = "iPhone 17 Pro"
    max_budget: Money = 145_000
    preferred_delivery_days: StrictInt = Field(default=2, ge=1, le=14)
    priorities: list[StrictStr] = Field(default_factory=lambda: ["value", "warranty", "fast delivery"])
    desired_freebies: list[StrictStr] = Field(default_factory=lambda: ["warranty", "express"])
    customer_id: StrictStr = "buyer_demo"
    customer_segment: StrictStr = "retail"
    request_message: StrictStr = Field(default="Find the smartest value package.", max_length=500)


class ProposedOffer(StrictModel):
    offer_id: StrictStr = Field(default_factory=lambda: f"offer_{uuid4().hex[:12]}")
    product_id: StrictStr
    offered_price: Money = Field(ge=1)
    included_concession_ids: list[StrictStr] = Field(default_factory=list)
    delivery_days: StrictInt = Field(ge=1, le=14)
    justification: StrictStr = Field(min_length=3, max_length=400)
    negotiation_round: StrictInt = Field(ge=1, le=10)


class OfferEconomics(StrictModel):
    concession_cost: Money
    customer_value: Money
    payment_cost: Money
    gross_profit: StrictInt
    profit_margin_bps: StrictInt
    direct_discount: Money


class PolicyIssue(StrictModel):
    code: StrictStr
    severity: Severity
    message: StrictStr


class DealDecision(StrictModel):
    approved: StrictBool
    status: DecisionStatus
    decision_code: StrictStr
    reason: StrictStr
    risk_score: StrictInt = Field(ge=0, le=100)
    policy_version: StrictStr
    issues: list[PolicyIssue] = Field(default_factory=list)
    economics: OfferEconomics


class AuthorizedDeal(StrictModel):
    deal_id: StrictStr = Field(default_factory=lambda: f"deal_{uuid4().hex}")
    product_id: StrictStr
    product_name: StrictStr
    final_price: Money
    concession_ids: list[StrictStr]
    merchant_id: StrictStr
    buyer_id: StrictStr
    policy_version: StrictStr
    capability_id: StrictStr
    approved_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    authorization_id: StrictStr = Field(default_factory=lambda: f"auth_{uuid4().hex}")
    status: DealStatus = DealStatus.AUTHORIZED
    signature: StrictStr = ""


class PaymentCreateRequest(StrictModel):
    deal_id: StrictStr
    authorization_id: StrictStr
    signature: StrictStr
    idempotency_key: StrictStr = Field(min_length=8, max_length=128)


class PaymentVerifyRequest(StrictModel):
    deal_id: StrictStr
    razorpay_order_id: StrictStr
    razorpay_payment_id: StrictStr
    razorpay_signature: StrictStr


class ApiError(StrictModel):
    error_code: StrictStr
    message: StrictStr
    request_id: StrictStr
    timestamp: datetime = Field(default_factory=utc_now)
