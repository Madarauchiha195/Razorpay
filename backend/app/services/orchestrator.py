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


# One buyer chat message is one guarded negotiation round, so a conversation can never become an
# unbounded stream of LLM calls or authorizations. The daily concession budget is the financial
# ceiling; this is the conversational one.
MAX_CHAT_TURNS = 12


def _append_stage(name: str):
    def transition(state: NegotiationGraphState) -> dict[str, list[str]]:
        return {"stages": [*state["stages"], name]}
    return transition


class NegotiationCoordinator:
    """The bounded state machine between proposal generation and DealGuard authorization."""

    def __init__(self, repository: DealMeshRepository, *, offline: bool = False):
        self.repository = repository
        # `offline=True` is the explicit opt-out for tests. Without it MerchantAgent auto-detects a
        # provider from settings, so building a coordinator would quietly become a network client.
        self.merchant_agent = MerchantAgent(offline=offline)
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

    def chat_turn(self, session_id: str, message: str) -> dict[str, Any]:
        """Continue an existing negotiation with one buyer message.

        Chat is a negotiation round, not a side channel. The buyer's words become the intent for
        a fresh merchant proposal, and that proposal passes through exactly the same DealGuard
        evaluation as every automatic round - so a price that moves in the chat box is always a
        price the guard has already approved. The message grants nothing: it reaches the model as
        untrusted preference text and every number is re-derived here.
        """
        session = self.repository.get_session(session_id)
        if session is None:
            raise KeyError(session_id)

        turn = self.repository.count_events(session_id, "BUYER_MESSAGE") + 1
        if turn > MAX_CHAT_TURNS:
            return self._chat_reply(
                session_id, turn, "LIMIT_REACHED",
                "We've reached the conversation limit for this negotiation. Start a new one to keep going.",
                code="CHAT_TURN_LIMIT",
            )
        self.repository.add_event(
            session_id, "BUYER_MESSAGE", "INFO", "Buyer sent a follow-up message in the live conversation.",
            payload={"turn": turn, "length": len(message)},
        )

        try:
            policy = self.repository.get_policy(session.product_id)
        except KeyError:
            return self._chat_reply(
                session_id, turn, "FAILED",
                "That product is no longer available for autonomous negotiation.",
                code="PRODUCT_UNAVAILABLE",
            )

        prior = [
            row for row in (self.repository.get_deal(deal_id) for deal_id in self.repository.session_deal_ids(session_id))
            if row is not None
        ]
        # Once money has moved, re-opening the price would be a second sale on one negotiation.
        if any(row.status in {DealStatus.PAYMENT_CREATED.value, DealStatus.PAID.value} for row in prior):
            return self._chat_reply(
                session_id, turn, "CLOSED",
                "This deal has already gone to payment, so I can't reopen the price. Start a new negotiation for another purchase.",
                code="DEAL_ALREADY_SETTLED",
            )

        # Still-live offers from earlier in this same conversation. Their concession cost was
        # charged to today's budget, so it is credited back when they are retired below.
        live = [row for row in prior if row.status in {DealStatus.AUTHORIZED.value, DealStatus.PENDING_APPROVAL.value}]
        refund = sum(self._concession_cost(policy, row) for row in live if row.status == DealStatus.AUTHORIZED.value)

        stored = dict(session.intent)
        stored["request_message"] = message[:500]
        intent = BuyerIntent.model_validate(stored)

        usage_data = self.repository.get_usage()
        usage = GuardUsage(
            # Netting the refund out first stops one conversation from charging itself repeatedly
            # for a package it only ever gave away once.
            daily_concession_cost=max(0, int(usage_data["daily_concession_cost"]) - refund),
            transactions_today=int(usage_data["transactions_today"]),
            agent_frozen=bool(usage_data["agent_frozen"]),
        )
        capability = self.capability_for(policy)
        # Rounds are capped by policy; a longer conversation keeps negotiating at the final round
        # rather than tripping MAX_NEGOTIATION_ROUNDS on the buyer's behalf.
        round_number = min(turn, policy.max_negotiation_rounds)

        proposal = self.merchant_agent.propose(policy, intent, round_number, "")
        offer = proposal.offer
        if proposal.error:
            self.repository.add_event(
                session_id, "LLM_FALLBACK", "WARNING",
                f"Live proposal engine unavailable, used the deterministic engine. {proposal.error}",
                "LLM_UNAVAILABLE",
            )
        decision = self.guard.evaluate(offer, policy, capability, usage)
        self.repository.add_event(
            session_id, "OFFER_PROPOSED", "INFO", f"Merchant agent answered the buyer via {proposal.engine}.",
            payload={"offer_id": offer.offer_id, "turn": turn, "round": round_number, "price": offer.offered_price, "engine": proposal.engine},
        )
        attribution = {"engine": proposal.engine, "live_llm": proposal.llm_used}

        if decision.status == DecisionStatus.BLOCKED:
            state = self.repository.register_block()
            self.repository.add_event(
                session_id, "DEALGUARD_BLOCK", "BLOCKED", decision.reason, decision.decision_code,
                payload={"offer_id": offer.offer_id, "turn": turn, "risk_score": decision.risk_score},
            )
            # No price is returned: a blocked proposal never becomes a number the buyer can see,
            # so the chat box cannot display a price DealGuard refused.
            return self._chat_reply(
                session_id, turn, "BLOCKED",
                f"I can't do that. {decision.reason} Nothing was authorized and no payment route was opened.",
                code=decision.decision_code, risk_score=decision.risk_score,
                agent_frozen=bool(state["agent_frozen"]), **attribution,
            )

        self._supersede(session_id, live)
        if refund:
            self.repository.add_daily_concession_cost(-refund)
        public_offer = self._public_offer(offer, policy, decision.economics.customer_value)

        if decision.status == DecisionStatus.REVIEW_REQUIRED:
            deal = self._create_deal(offer, policy, capability, DealStatus.PENDING_APPROVAL, intent.customer_id)
            self.repository.add_deal(deal.model_dump(mode="json"), deal.status.value, deal.signature, deal.expires_at)
            self.repository.add_event(
                session_id, "HUMAN_REVIEW", "WARNING", decision.reason, decision.decision_code,
                payload={"deal_id": deal.deal_id, "turn": turn},
            )
            self.repository.set_session_status(session_id, "PENDING_APPROVAL")
            return self._chat_reply(
                session_id, turn, "REVIEW_REQUIRED",
                f"{offer.justification} At that price a person at the merchant has to sign off, so I've queued it for approval.",
                code=decision.decision_code, offer=public_offer, deal=self._public_deal(deal), **attribution,
            )

        # The buyer is speaking for themselves here, so BuyerAgent.assess is deliberately not
        # consulted: auto-declining on behalf of a human who just typed would be wrong.
        deal = self._create_deal(offer, policy, capability, DealStatus.AUTHORIZED, intent.customer_id)
        self.repository.add_deal(deal.model_dump(mode="json"), deal.status.value, deal.signature, deal.expires_at)
        self.repository.add_daily_concession_cost(decision.economics.concession_cost)
        self.repository.add_event(
            session_id, "DEAL_AUTHORIZED", "APPROVED", "DealGuard created a signed authorization from the live conversation.",
            "DEAL_APPROVED", payload={"deal_id": deal.deal_id, "turn": turn},
        )
        self.repository.set_session_status(session_id, "AUTHORIZED")
        return self._chat_reply(
            session_id, turn, "AUTHORIZED", offer.justification,
            code=decision.decision_code, offer=public_offer, deal=self._public_deal(deal), **attribution,
        )

    @staticmethod
    def _chat_reply(
        session_id: str, turn: int, status: str, reply: str, *, code: str | None = None,
        offer: dict[str, Any] | None = None, deal: dict[str, Any] | None = None,
        engine: str | None = None, live_llm: bool = False, risk_score: int | None = None,
        agent_frozen: bool | None = None,
    ) -> dict[str, Any]:
        """One uniform chat-turn envelope, so the UI never has to branch on shape."""
        return {
            "session_id": session_id, "turn": turn, "status": status, "reply": reply, "code": code,
            "offer": offer, "deal": deal, "price": offer["offered_price"] if offer else None,
            "engine": engine, "live_llm": live_llm, "risk_score": risk_score, "agent_frozen": agent_frozen,
        }

    def _supersede(self, session_id: str, rows: list[Any]) -> None:
        """Retire the earlier authorizations from this conversation.

        Without this every chat turn would leave another signed, payable deal behind, and a buyer
        could keep re-asking until one round landed low and then pay that one. Only the newest
        offer in a conversation stays payable.
        """
        for row in rows:
            self.repository.update_deal_status(row.deal_id, DealStatus.EXPIRED.value)
            self.repository.add_event(
                session_id, "DEAL_SUPERSEDED", "INFO",
                "An earlier offer in this conversation was retired when the buyer kept negotiating.",
                "DEAL_SUPERSEDED", payload={"deal_id": row.deal_id},
            )

    @staticmethod
    def _concession_cost(policy: MerchantPolicy, row: Any) -> int:
        """Merchant cost of a stored deal's package, re-derived from that product's own policy."""
        ids = set(row.data.get("concession_ids") or [])
        return sum(item.merchant_cost for item in policy.concessions if item.id in ids)

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
