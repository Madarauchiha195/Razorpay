from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, AsyncIterator, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from ..agents import BuyerAgent, MerchantAgent
from ..config import settings
from ..db import DealMeshRepository
from ..dealguard import DealGuard
from ..dealguard.engine import GuardUsage
from ..domain.models import (
    AgentCapability,
    AuthorizedDeal,
    BuyerIntent,
    DealStatus,
    DecisionStatus,
    MerchantPolicy,
    ProposedOffer,
    utc_now,
)
from ..security import sign_deal


class NegotiationGraphState(TypedDict):
    stages: list[str]


def _append_stage(name: str):
    def transition(state: NegotiationGraphState) -> dict[str, list[str]]:
        return {"stages": [*state["stages"], name]}
    return transition


class NegotiationCoordinator:
    """The bounded state machine between proposal generation and DealGuard authorization."""

    def __init__(self, repository: DealMeshRepository):
        self.repository = repository
        self.merchant_agent = MerchantAgent()
        self.buyer_agent = BuyerAgent()
        self.guard = DealGuard()
        # LangGraph makes the permitted hand-off sequence explicit and inspectable.
        graph = StateGraph(NegotiationGraphState)
        graph.add_node("parse_buyer_intent", _append_stage("PARSE_BUYER_INTENT"))
        graph.add_node("generate_merchant_offer", _append_stage("GENERATE_MERCHANT_OFFER"))
        graph.add_node("dealguard_check", _append_stage("DEALGUARD_CHECK"))
        graph.add_edge(START, "parse_buyer_intent")
        graph.add_edge("parse_buyer_intent", "generate_merchant_offer")
        graph.add_edge("generate_merchant_offer", "dealguard_check")
        graph.add_edge("dealguard_check", END)
        self.graph = graph.compile()

    @staticmethod
    def capability_for(policy: MerchantPolicy) -> AgentCapability:
        return AgentCapability(
            capability_id=f"cap_{policy.product_id}_{policy.policy_version}",
            merchant_id=policy.merchant_id,
            allowed_products=[policy.product_id],
            max_discount=policy.max_discount,
            min_price=policy.min_acceptable_price,
            max_concession_budget=policy.max_freebie_value,
            max_transactions=policy.max_transactions,
            expires_at=policy.authorization_expires_at,
            policy_version=policy.policy_version,
        )

    async def stream(self, intent: BuyerIntent) -> AsyncIterator[dict[str, Any]]:
        # This declarative trace is used for audit visibility; financial choices remain in the
        # deterministic per-round loop below, where retries and payment authorization are bounded.
        graph_trace = self.graph.invoke({"stages": []})["stages"]
        session_id = f"neg_{uuid4().hex[:16]}"
        self.repository.create_session(session_id, intent.customer_id, intent.product_id, intent.model_dump(mode="json"))

        # Policy is loaded for the product the buyer actually chose. An unknown product is a
        # hard stop: without merchant-owned policy there is nothing to authorize against.
        try:
            policy = self.repository.get_policy(intent.product_id)
        except KeyError:
            payload = {
                "type": "NEGOTIATION_FAILED", "session_id": session_id, "code": "PRODUCT_UNAVAILABLE",
                "message": "That product is unavailable for autonomous negotiation.",
            }
            self.repository.add_event(session_id, "NEGOTIATION_FAILED", "BLOCKED", payload["message"], payload["code"])
            self.repository.set_session_status(session_id, "FAILED")
            yield payload
            return

        yield {
            "type": "NEGOTIATION_STARTED", "session_id": session_id,
            "message": f"Buyer intent received for {policy.product_name}. Starting bounded negotiation with {self.merchant_agent.mode}.",
        }
        await asyncio.sleep(0.15)

        self.repository.add_event(session_id, "BUYER_INTENT", "INFO", "Buyer intent parsed without exposing merchant policy.")
        yield {
            "type": "BUYER_INTENT_PARSED", "session_id": session_id,
            "message": f"Buyer agent ranked value, protection, and delivery priorities ({graph_trace[0]}).",
        }
        await asyncio.sleep(0.15)

        capability = self.capability_for(policy)
        feedback = ""
        for round_number in range(1, policy.max_negotiation_rounds + 1):
            usage_data = self.repository.get_usage()
            usage = GuardUsage(
                daily_concession_cost=int(usage_data["daily_concession_cost"]),
                transactions_today=int(usage_data["transactions_today"]),
                agent_frozen=bool(usage_data["agent_frozen"]),
            )
            yield {"type": "ROUND_STARTED", "session_id": session_id, "round": round_number, "message": f"Round {round_number}: {self.merchant_agent.mode} is composing a value package for {policy.product_name}."}
            await asyncio.sleep(0.18)

            proposal = self.merchant_agent.propose(policy, intent, round_number, feedback)
            offer = proposal.offer
            if proposal.error:
                # Surfaced instead of swallowed: a bad key, retired model, or rate limit is now
                # visible in the audit trail rather than silently degrading to the offline engine.
                self.repository.add_event(
                    session_id, "LLM_FALLBACK", "WARNING",
                    f"Live proposal engine unavailable, used the deterministic engine. {proposal.error}",
                    "LLM_UNAVAILABLE",
                )
            decision = self.guard.evaluate(offer, policy, capability, usage)
            self.repository.add_event(
                session_id, "OFFER_PROPOSED", "INFO", f"Merchant agent proposed a package via {proposal.engine}.",
                payload={"offer_id": offer.offer_id, "round": round_number, "price": offer.offered_price, "engine": proposal.engine},
            )
            yield {
                "type": "OFFER_PROPOSED", "session_id": session_id, "round": round_number,
                "offer": self._public_offer(offer, policy, decision.economics.customer_value),
                "engine": proposal.engine, "live_llm": proposal.llm_used,
                "message": f"Offer composed by {proposal.engine}.",
            }
            await asyncio.sleep(0.18)

            if decision.status == DecisionStatus.BLOCKED:
                state = self.repository.register_block()
                self.repository.add_event(
                    session_id, "DEALGUARD_BLOCK", "BLOCKED", decision.reason, decision.decision_code,
                    payload={"offer_id": offer.offer_id, "risk_score": decision.risk_score},
                )
                yield {
                    "type": "DEALGUARD_BLOCK", "session_id": session_id, "round": round_number,
                    "code": decision.decision_code, "message": "DealGuard blocked this proposal. No payment route was opened.",
                    "risk_score": decision.risk_score, "agent_frozen": bool(state["agent_frozen"]),
                }
                feedback = decision.decision_code
                await asyncio.sleep(0.22)
                continue

            if decision.status == DecisionStatus.REVIEW_REQUIRED:
                deal = self._create_deal(offer, policy, capability, DealStatus.PENDING_APPROVAL, intent.customer_id)
                self.repository.add_deal(deal.model_dump(mode="json"), deal.status.value, deal.signature, deal.expires_at)
                self.repository.add_event(session_id, "HUMAN_REVIEW", "WARNING", decision.reason, decision.decision_code, payload={"deal_id": deal.deal_id})
                self.repository.set_session_status(session_id, "PENDING_APPROVAL")
                yield {"type": "HUMAN_REVIEW_REQUIRED", "session_id": session_id, "message": decision.reason, "deal": self._public_deal(deal), "offer": self._public_offer(offer, policy, decision.economics.customer_value)}
                return

            assessment = self.buyer_agent.assess(intent, offer, decision.economics.customer_value)
            if not assessment.should_continue:
                self.repository.add_event(session_id, "BUYER_DECLINED", "INFO", assessment.message)
                yield {"type": "NEGOTIATION_FAILED", "session_id": session_id, "code": "BUYER_DECLINED", "message": assessment.message}
                self.repository.set_session_status(session_id, "FAILED")
                return

            deal = self._create_deal(offer, policy, capability, DealStatus.AUTHORIZED, intent.customer_id)
            self.repository.add_deal(deal.model_dump(mode="json"), deal.status.value, deal.signature, deal.expires_at)
            self.repository.add_daily_concession_cost(decision.economics.concession_cost)
            self.repository.add_event(session_id, "DEAL_AUTHORIZED", "APPROVED", "DealGuard created a signed authorization.", "DEAL_APPROVED", payload={"deal_id": deal.deal_id})
            self.repository.set_session_status(session_id, "AUTHORIZED")
            yield {"type": "DEAL_AUTHORIZED", "session_id": session_id, "message": "DealGuard authorized this signed, time-limited deal.", "deal": self._public_deal(deal), "offer": self._public_offer(offer, policy, decision.economics.customer_value)}
            return

        self.repository.add_event(session_id, "NEGOTIATION_FAILED", "BLOCKED", "Maximum rounds reached without authorization.", "MAX_NEGOTIATION_ROUNDS")
        self.repository.set_session_status(session_id, "FAILED")
        yield {"type": "NEGOTIATION_FAILED", "session_id": session_id, "code": "MAX_NEGOTIATION_ROUNDS", "message": "No agreement was authorized within the merchant's negotiation limits."}

    def _create_deal(self, offer: ProposedOffer, policy: MerchantPolicy, capability: AgentCapability, status: DealStatus, buyer_id: str) -> AuthorizedDeal:
        deal = AuthorizedDeal(
            product_id=policy.product_id,
            product_name=policy.product_name,
            final_price=offer.offered_price,
            concession_ids=offer.included_concession_ids,
            merchant_id=policy.merchant_id,
            buyer_id=buyer_id,
            policy_version=policy.policy_version,
            capability_id=capability.capability_id,
            expires_at=utc_now() + timedelta(minutes=settings.deal_expiry_minutes),
            status=status,
        )
        return deal.model_copy(update={"signature": sign_deal(deal, settings.deal_signing_secret)})

    @staticmethod
    def _public_offer(offer: ProposedOffer, policy: MerchantPolicy, customer_value: int) -> dict[str, Any]:
        names = {item.id: item.name for item in policy.concessions}
        return {
            "offer_id": offer.offer_id, "product_id": offer.product_id, "offered_price": offer.offered_price,
            "concessions": [names[item_id] for item_id in offer.included_concession_ids if item_id in names],
            "delivery_days": offer.delivery_days, "justification": offer.justification,
            "negotiation_round": offer.negotiation_round, "estimated_customer_value": customer_value,
        }

    @staticmethod
    def _public_deal(deal: AuthorizedDeal) -> dict[str, Any]:
        return {
            "deal_id": deal.deal_id, "product_id": deal.product_id, "product_name": deal.product_name,
            "final_price": deal.final_price, "concession_ids": deal.concession_ids,
            "policy_version": deal.policy_version, "capability_id": deal.capability_id,
            "authorization_id": deal.authorization_id, "expires_at": deal.expires_at.isoformat(),
            "status": deal.status.value, "signature": deal.signature,
        }
