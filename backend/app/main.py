from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .agents.providers import GroqProvider
from .db import DealMeshRepository
from .domain.models import (
    AuthorizedDeal,
    BuyerIntent,
    DealStatus,
    MerchantPolicy,
    PaymentCreateRequest,
    PaymentVerifyRequest,
    ProposedOffer,
)
from .dealguard.engine import GuardUsage
from .payments import RazorpayService
from .security import verify_deal_signature
from .services import NegotiationCoordinator


repository = DealMeshRepository(settings.database_url)
coordinator = NegotiationCoordinator(repository)
razorpay = RazorpayService()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    repository.initialise()
    yield


app = FastAPI(title="DealMesh API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # 5173 is `vite dev`; 4173 is `vite preview`, which is what docker-compose serves.
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "Idempotency-Key"],
)


class BuyerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=500)


def error(code: str, message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error_code": code, "message": message})


def _policy_or_404(product_id: str | None) -> MerchantPolicy:
    """Load merchant policy for a product, or 404 rather than silently substituting another."""
    try:
        return repository.get_policy(product_id)
    except KeyError:
        raise error("PRODUCT_NOT_FOUND", f"No merchant policy exists for product '{product_id}'.", 404) from None


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"error_code": "REQUEST_FAILED", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={**detail, "request_id": request.headers.get("X-Request-ID", "local")})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "dealmesh"}


@app.get("/api/products")
def products() -> list[dict[str, object]]:
    """The full negotiable catalog, without merchant-confidential economics."""
    return repository.list_products()


@app.get("/api/catalog")
def catalog(product_id: str | None = None) -> dict[str, object]:
    policy = _policy_or_404(product_id)
    return {
        "product_id": policy.product_id, "product_name": policy.product_name, "listing_price": policy.target_price,
        "concessions": [{"id": item.id, "name": item.name, "customer_value": item.customer_perceived_value} for item in policy.concessions if item.allowed and item.inventory_available],
        "products": repository.list_products(),
    }


@app.post("/api/negotiation/start")
async def start_negotiation(intent: BuyerIntent) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        async for payload in coordinator.stream(intent):
            yield f"data: {json.dumps(payload)}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/negotiation/{session_id}/message")
def add_message(session_id: str, body: BuyerMessage) -> dict[str, str]:
    session = repository.get_session(session_id)
    if not session:
        raise error("SESSION_NOT_FOUND", "Negotiation session was not found.", 404)
    repository.add_event(session_id, "BUYER_MESSAGE", "INFO", "Buyer sent a follow-up message.", payload={"length": len(body.message)})
    # The message is audit-safe; it cannot alter policy, deals, or payment authority.
    return {"status": "recorded", "message": "Buyer message recorded for the next bounded negotiation turn."}


@app.post("/api/offers/{offer_id}/validate")
def validate_offer(offer_id: str, offer: ProposedOffer) -> dict[str, object]:
    """Merchant-side preview endpoint. It validates but never creates an authorized deal."""
    if offer.offer_id != offer_id:
        raise error("OFFER_ID_MISMATCH", "Path offer ID does not match the structured offer payload.")
    policy = _policy_or_404(offer.product_id)
    usage = repository.get_usage()
    decision = coordinator.guard.evaluate(
        offer, policy, coordinator.capability_for(policy),
        GuardUsage(
            daily_concession_cost=int(usage["daily_concession_cost"]),
            transactions_today=int(usage["transactions_today"]),
            agent_frozen=bool(usage["agent_frozen"]),
        ),
    )
    repository.add_event(None, "OFFER_VALIDATED", decision.status.value, decision.reason, decision.decision_code, {"offer_id": offer_id})
    return decision.model_dump(mode="json")


@app.get("/api/negotiation/{session_id}")
def get_negotiation(session_id: str) -> dict[str, object]:
    session = repository.get_session(session_id)
    if not session:
        raise error("SESSION_NOT_FOUND", "Negotiation session was not found.", 404)
    return {"session_id": session.id, "status": session.status, "product_id": session.product_id, "created_at": session.created_at.isoformat()}


@app.get("/api/merchant/policy")
def get_policy(product_id: str | None = None) -> MerchantPolicy:
    return _policy_or_404(product_id)


@app.put("/api/merchant/policy")
def update_policy(policy: MerchantPolicy) -> MerchantPolicy:
    updated = repository.save_policy(policy)
    repository.audit("merchant", "POLICY_UPDATED", "APPROVED", data={"product_id": updated.product_id, "policy_version": updated.policy_version})
    return updated


@app.get("/api/merchant/dashboard")
def merchant_dashboard() -> dict[str, object]:
    return repository.dashboard()


@app.get("/api/merchant/activity")
def merchant_activity(days: int = 7) -> list[dict[str, object]]:
    """Real per-day negotiation, authorization, block, and value series."""
    return repository.daily_activity(min(max(days, 1), 30))


@app.get("/api/merchant/block-reasons")
def merchant_block_reasons() -> list[dict[str, object]]:
    """Real distribution of the DealGuard codes that actually fired."""
    return repository.block_reasons()


@app.get("/api/dealguard/events")
def guard_events(limit: int = 30) -> list[dict[str, object]]:
    return repository.list_events(min(max(limit, 1), 100))


@app.get("/api/agent/state")
def agent_state() -> dict[str, object]:
    """Authoritative agent and daily-usage state, with today's counters already rolled over."""
    return repository.agent_state()


@app.post("/api/agent/freeze")
def freeze_agent() -> dict[str, bool]:
    repository.set_agent_frozen(True)
    repository.add_event(None, "AGENT_FROZEN", "BLOCKED", "Merchant manually froze autonomous negotiation.", "AGENT_FROZEN")
    return {"agent_frozen": True}


@app.post("/api/agent/reactivate")
def reactivate_agent() -> dict[str, bool]:
    repository.set_agent_frozen(False)
    repository.add_event(None, "AGENT_REACTIVATED", "INFO", "Merchant reactivated autonomous negotiation.")
    return {"agent_frozen": False}


@app.post("/api/agent/reset-daily-usage")
def reset_daily_usage() -> dict[str, object]:
    """Operator escape hatch: clear today's concession spend, transaction, and violation counters.

    Counters also roll over on their own at the start of each UTC day; this is for recovering
    a session immediately instead of waiting.
    """
    state = repository.reset_daily_usage()
    repository.add_event(None, "DAILY_USAGE_RESET", "INFO", "Merchant reset the daily concession and violation counters.")
    return state


@app.get("/api/llm/status")
def llm_status(probe: bool = True) -> dict[str, object]:
    """Diagnose the live proposal engine.

    Set probe=false for configuration only. With probe=true this sends one real request, so
    a wrong key, a retired model name, or an exhausted rate limit is reported explicitly
    instead of silently degrading to the deterministic engine.
    """
    provider = coordinator.merchant_agent.provider
    status: dict[str, object] = {
        "provider": settings.llm_provider,
        "live_llm_configured": provider is not None,
        "engine_name": coordinator.merchant_agent.mode,
        "groq_key_present": bool(settings.groq_api_key),
        "groq_model_requested": settings.groq_model,
        "ollama_base_url": settings.ollama_base_url,
        "ollama_model": settings.ollama_model,
        "temperature": settings.llm_temperature,
        "timeout_seconds": settings.llm_timeout_seconds,
    }
    if provider is None:
        status["probe"] = "skipped"
        status["hint"] = "Set GROQ_API_KEY in backend/.env (or LLM_PROVIDER=ollama) to enable live proposals."
        return status
    if not probe:
        status["probe"] = "skipped"
        return status

    if isinstance(provider, GroqProvider):
        # The authoritative answer to "which models can this key use?". A pinned-but-retired model
        # name is the most common reason a valid key still produces no live proposals.
        status["groq_models_available"] = provider.discover_models(refresh=True)
        status["groq_discovery_error"] = provider.discovery_error
        status["groq_models_tried_in_order"] = provider.candidate_models()

    policy = repository.get_policy()
    intent = BuyerIntent(
        product_id=policy.product_id, product_name=policy.product_name,
        max_budget=policy.target_price, preferred_delivery_days=2,
        priorities=["value"], desired_freebies=[], customer_id="diagnostic",
        customer_segment="retail", request_message="Connectivity probe.",
    )
    outcome = provider.generate(policy, intent, 1, "")
    status["probe"] = "ok" if outcome.ok else "failed"
    status["model_served"] = outcome.model
    status["error"] = outcome.error
    if outcome.ok and outcome.offer is not None:
        status["sample_price"] = outcome.offer.offered_price
        status["sample_justification"] = outcome.offer.justification
    return status


def _load_deal_or_404(deal_id: str) -> tuple[AuthorizedDeal, str]:
    row = repository.get_deal(deal_id)
    if row is None:
        raise error("DEAL_NOT_FOUND", "Authorized deal was not found.", 404)
    deal = AuthorizedDeal.model_validate(row.data)
    return deal, row.status


@app.get("/api/deals/{deal_id}")
def get_deal(deal_id: str) -> dict[str, object]:
    deal, stored_status = _load_deal_or_404(deal_id)
    return coordinator._public_deal(deal.model_copy(update={"status": DealStatus(stored_status)}))


@app.post("/api/deals/{deal_id}/approve")
def approve_deal(deal_id: str) -> dict[str, object]:
    deal, status = _load_deal_or_404(deal_id)
    if status != DealStatus.PENDING_APPROVAL.value:
        raise error("DEAL_NOT_PENDING_APPROVAL", "This deal is not waiting for human approval.")
    if deal.expires_at <= datetime.now(timezone.utc):
        repository.update_deal_status(deal_id, DealStatus.EXPIRED.value)
        raise error("DEAL_EXPIRED", "This deal has expired.")
    repository.update_deal_status(deal_id, DealStatus.AUTHORIZED.value)
    repository.add_event(None, "HUMAN_APPROVED", "APPROVED", "Merchant approved a review-gated deal.", "DEAL_APPROVED", {"deal_id": deal_id})
    return {"status": "AUTHORIZED", "deal": coordinator._public_deal(deal.model_copy(update={"status": DealStatus.AUTHORIZED}))}


@app.post("/api/deals/{deal_id}/authorize")
def authorize_deal(deal_id: str) -> dict[str, object]:
    """Compatibility route for the explicit merchant approval step in a review-gated deal."""
    deal, status = _load_deal_or_404(deal_id)
    if status == DealStatus.AUTHORIZED.value:
        return {"status": "AUTHORIZED", "deal": coordinator._public_deal(deal.model_copy(update={"status": DealStatus.AUTHORIZED}))}
    return approve_deal(deal_id)


@app.post("/api/payments/create")
def create_payment(request: PaymentCreateRequest) -> dict[str, object]:
    deal, stored_status = _load_deal_or_404(request.deal_id)
    if deal.authorization_id != request.authorization_id or deal.signature != request.signature:
        raise error("DEAL_INTEGRITY_FAILED", "Deal authorization could not be verified.")
    if not verify_deal_signature(deal, settings.deal_signing_secret):
        raise error("DEAL_INTEGRITY_FAILED", "Deal signature verification failed.")
    if deal.expires_at <= datetime.now(timezone.utc):
        repository.update_deal_status(deal.deal_id, DealStatus.EXPIRED.value)
        raise error("DEAL_EXPIRED", "Deal authorization has expired.")
    if stored_status != DealStatus.AUTHORIZED.value:
        code = "DEAL_ALREADY_USED" if stored_status in {DealStatus.PAYMENT_CREATED.value, DealStatus.PAID.value} else "DEAL_NOT_AUTHORIZED"
        raise error(code, "This deal is not available for a new payment.")
    if repository.get_payment(deal.deal_id):
        raise error("DUPLICATE_PAYMENT", "A payment order already exists for this deal.")
    try:
        order = razorpay.create_order(deal)
    except RuntimeError:
        raise error("PAYMENT_PROVIDER_UNAVAILABLE", "Unable to create a Razorpay test order.", 502)
    repository.create_payment(deal.deal_id, request.idempotency_key, order.order_id)
    repository.add_event(None, "PAYMENT_ORDER_CREATED", "APPROVED", "A payment order was created from a signed authorized deal.", "PAYMENT_READY", {"deal_id": deal.deal_id})
    return {"order_id": order.order_id, "amount": deal.final_price * 100, "currency": "INR", "key_id": order.key_id, "live_checkout": order.is_live_checkout, "deal_id": deal.deal_id}


@app.post("/api/payments/verify")
def verify_payment(request: PaymentVerifyRequest) -> dict[str, str]:
    deal, status = _load_deal_or_404(request.deal_id)
    payment = repository.get_payment(deal.deal_id)
    if not payment or payment.provider_order_id != request.razorpay_order_id or status != DealStatus.PAYMENT_CREATED.value:
        raise error("PAYMENT_VERIFICATION_FAILED", "Payment does not match an active authorized order.")
    if not razorpay.verify_payment_signature(request.razorpay_order_id, request.razorpay_payment_id, request.razorpay_signature):
        raise error("PAYMENT_VERIFICATION_FAILED", "Payment signature verification failed.")
    repository.mark_paid(deal.deal_id, request.razorpay_payment_id)
    repository.add_event(None, "PAYMENT_VERIFIED", "APPROVED", "Razorpay payment signature verified.", "PAYMENT_PAID", {"deal_id": deal.deal_id})
    return {"status": "PAID", "deal_id": deal.deal_id}


@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> dict[str, str]:
    payload = await request.body()
    if not razorpay.verify_webhook(payload, request.headers.get("X-Razorpay-Signature")):
        raise error("WEBHOOK_SIGNATURE_INVALID", "Webhook signature verification failed.", 401)
    return {"status": "accepted"}
