from __future__ import annotations

import json
import logging
import random
import urllib.error
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen

from pydantic import ValidationError

from ..config import settings
from ..domain.models import BuyerIntent, MerchantPolicy, ProposedOffer

logger = logging.getLogger("dealmesh.llm")


# Groq retires hosted model IDs periodically, and a single pinned name is the most common reason
# a working key appears to "do nothing" - every proposal 404s and silently falls back.
#
# Rather than ship a hardcoded list that rots, GroqProvider asks the account which models it can
# actually use (GET /openai/v1/models) and tries those. These names are only a last resort for
# when discovery itself fails; they are not guaranteed current. `GET /api/llm/status` reports the
# live list, which is the authoritative answer for any given key.
GROQ_MODEL_FALLBACKS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Audio, embedding, moderation, and safety models are listed alongside chat models but cannot
# answer a JSON offer brief, so they are skipped during discovery. "orpheus" is Canopy Labs'
# text-to-speech family, which Groq lists without any marker in the name.
_NON_CHAT_MARKERS = ("whisper", "tts", "embed", "guard", "moderation", "rerank", "orpheus")

# GPT-OSS and Qwen3 are *reasoning* models: they emit chain-of-thought before the answer, and those
# tokens count against max_tokens. A budget sized for the JSON alone gets consumed by the reasoning
# and the response is truncated, which Groq reports as HTTP 400 "json_validate_failed" with an
# EMPTY failed_generation - a confusing error that looks like a prompt bug.
# Measured 2026-08-23 on openai/gpt-oss-20b: 400 tokens -> 1/4 requests succeeded; 1200 -> 4/4.
_MAX_COMPLETION_TOKENS = 1200


@dataclass
class ProviderOutcome:
    """The result of one proposal attempt, including why it failed if it did."""

    offer: ProposedOffer | None
    engine: str
    error: str | None = None
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.offer is not None


def negotiable_band(policy: MerchantPolicy) -> tuple[int, int]:
    """The price band the merchant sanctions for disclosure to a proposal model.

    The lower bound is the cheapest auto-approvable price, which sits at or above the real
    floor. The model therefore never learns min_acceptable_price, base_cost, min_profit, or
    any concession's merchant_cost. DealGuard still re-validates independently.
    """
    floor = max(policy.human_approval_threshold + 1, policy.min_acceptable_price)
    ceiling = max(policy.target_price, floor)
    return floor, ceiling


def affordable_concession_count(policy: MerchantPolicy) -> int:
    """How many concessions fit the delegated budget, computed server-side.

    This lets us bound the model without ever telling it what a concession costs.
    """
    available = sorted(
        (item.merchant_cost for item in policy.concessions if item.allowed and item.inventory_available),
    )
    budget = policy.max_freebie_value
    count = 0
    for cost in available:
        if budget - cost < 0:
            break
        budget -= cost
        count += 1
    return count


def _public_brief(policy: MerchantPolicy, intent: BuyerIntent, round_number: int, feedback: str) -> dict[str, Any]:
    floor, ceiling = negotiable_band(policy)
    allowed = [
        {"id": item.id, "name": item.name, "customer_value": item.customer_perceived_value}
        for item in policy.concessions if item.allowed and item.inventory_available
    ]
    return {
        "task": "Propose one persuasive merchant offer as JSON. This proposal is not authorization.",
        "product": {"id": policy.product_id, "name": policy.product_name, "list_price": policy.target_price},
        "buyer_preferences": {
            "max_budget": intent.max_budget,
            "preferred_delivery_days": intent.preferred_delivery_days,
            "desired_concession_ids": intent.desired_freebies,
            "priorities": intent.priorities,
            # The buyer's own words, so a chat turn gets an answer instead of a generic pitch.
            # This is untrusted input: an injection attempt here can only change wording, because
            # coerce_offer clamps the price into the band below and DealGuard re-derives every
            # number afterwards. Nothing written here can authorize anything.
            "message": intent.request_message,
        },
        "offer_rules": {
            "round": round_number,
            "validator_feedback_code": feedback or None,
            "offered_price_min": floor,
            "offered_price_max": ceiling,
            "available_concessions": allowed,
            "max_concessions": affordable_concession_count(policy),
            "delivery_days_min": 1,
            "delivery_days_max": min(14, max(1, intent.preferred_delivery_days + 1)),
            "variation_seed": random.randint(1000, 9999),
            "instruction": (
                "Vary the price and the concession mix from round to round. Choose a price "
                "inside the min/max band. Write a fresh, specific justification each time; "
                "never reuse a previous sentence. Reply directly to buyer_preferences.message "
                "in the justification, as the merchant agent speaking to the buyer."
            ),
        },
        "response_contract": {
            "product_id": policy.product_id,
            "offered_price": "integer INR inside the offered_price band",
            "included_concession_ids": "array of available concession IDs, at most max_concessions",
            "delivery_days": "integer within the delivery band",
            "justification": "buyer-friendly text, 1-2 sentences, no merchant cost or policy details",
            "negotiation_round": round_number,
        },
    }


SYSTEM_PROMPT = (
    "You are a strict JSON-only merchant proposal generator. You have no payment, "
    "policy-update, or authorization authority. Never expose merchant costs, price floors, "
    "or policy thresholds. Respond with a single JSON object and nothing else."
)


def coerce_offer(raw: str, policy: MerchantPolicy, round_number: int) -> ProposedOffer:
    """Turn loose model JSON into a valid ProposedOffer, or raise.

    ProposedOffer is deliberately strict (extra="forbid", bounded string lengths), which a
    chatty model trips over constantly. Normalising here means a well-intentioned but sloppy
    response becomes a real proposal instead of a silent fallback. This only reshapes the
    proposal - it grants nothing. DealGuard still re-derives every number before authorizing.
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Model did not return a JSON object")

    # Some models nest the payload under a wrapper key.
    if "offer" in data and isinstance(data["offer"], dict):
        data = data["offer"]

    def as_int(value: Any, fallback: int) -> int:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return fallback

    floor, ceiling = negotiable_band(policy)
    price = as_int(data.get("offered_price", data.get("price", ceiling)), ceiling)
    # Clamping to the sanctioned band keeps a hallucinated number from wasting a round. A
    # price outside merchant policy would be blocked by DealGuard anyway.
    price = min(max(price, floor), ceiling)

    valid_ids = {item.id for item in policy.concessions if item.allowed and item.inventory_available}
    raw_ids = data.get("included_concession_ids", data.get("concessions", []))
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    concession_ids = [str(item) for item in raw_ids if isinstance(item, (str, int)) and str(item) in valid_ids]

    justification = str(data.get("justification") or "Value package composed for your stated priorities.").strip()
    justification = justification[:400] if len(justification) >= 3 else "Value package composed for your stated priorities."

    return ProposedOffer(
        product_id=policy.product_id,
        offered_price=price,
        included_concession_ids=list(dict.fromkeys(concession_ids)),
        delivery_days=min(max(as_int(data.get("delivery_days", 2), 2), 1), 14),
        justification=justification,
        negotiation_round=min(max(round_number, 1), 10),
    )


class LLMProvider(Protocol):
    name: str

    def generate(self, policy: MerchantPolicy, intent: BuyerIntent, round_number: int, feedback: str) -> ProviderOutcome:
        """Return a ProviderOutcome; a failed outcome carries the reason it failed."""


# Groq sits behind Cloudflare, which rejects urllib's default "Python-urllib/3.x" User-Agent with
# HTTP 403 "error code: 1010" before the request ever reaches the API. Without an explicit agent
# string the integration cannot work even with a perfectly valid key, and the failure looks
# identical to a bad key. Verified 2026-08-23: default UA -> 403/1010, explicit UA -> 401 from Groq.
_USER_AGENT = "DealMesh/1.0"


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT, **headers},
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - operator-configured endpoint
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": _USER_AGENT, **headers})
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - operator-configured endpoint
        return json.loads(response.read().decode("utf-8"))


def _describe(exc: Exception) -> str:
    """Turn a provider exception into something an operator can act on."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # pragma: no cover - defensive
            detail = ""
        hint = ""
        if exc.code == 401:
            hint = " (check GROQ_API_KEY)"
        elif exc.code == 403:
            hint = " (edge/WAF rejection rather than an auth failure if the body mentions code 1010)"
        elif exc.code == 404:
            hint = " (model name is probably retired - see GROQ_MODEL)"
        elif exc.code == 429:
            hint = " (free-tier rate limit reached; wait or switch model)"
        elif exc.code == 400 and "json_validate_failed" in detail:
            hint = " (reasoning model truncated before finishing the JSON - raise _MAX_COMPLETION_TOKENS)"
        return f"HTTP {exc.code}{hint}: {detail}"
    if isinstance(exc, urllib.error.URLError):
        return f"Network error: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return "Provider timed out"
    if isinstance(exc, ValidationError):
        return f"Model returned JSON that failed offer validation: {exc.error_count()} issue(s)"
    return f"{type(exc).__name__}: {exc}"


class GroqProvider:
    """Groq JSON-mode adapter. Errors are surfaced, never silently swallowed."""

    name = "Groq (hosted LLM)"

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.last_model: str | None = None
        self._discovered: list[str] | None = None
        self.discovery_error: str | None = None
        # Models the account is not entitled to use. Discovery lists them, but they reject every
        # request until an org admin accepts their terms in the Groq console. Remembering them
        # stops each later round from re-spending a network round-trip on a guaranteed failure.
        self._gated: set[str] = set()

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.groq_api_key}"}

    def discover_models(self, refresh: bool = False) -> list[str]:
        """Ask the account which chat models it can actually use.

        Lazy and cached on purpose. Doing this in __init__ would put a network round-trip inside
        application startup, so an unreachable or slow Groq would delay every boot - including
        boots that never go on to negotiate anything.
        """
        if self._discovered is not None and not refresh:
            return self._discovered
        try:
            output = _get_json(f"{GROQ_BASE_URL}/models", self._headers(), settings.llm_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - classified by _describe
            # Discovery failing is not fatal; it just means we fall back to the pinned names.
            self.discovery_error = _describe(exc)
            logger.warning("Groq model discovery failed -> %s", self.discovery_error)
            self._discovered = []
            return self._discovered

        self.discovery_error = None
        self._discovered = [
            str(entry["id"])
            for entry in output.get("data", [])
            if isinstance(entry, dict) and entry.get("id")
            and not any(marker in str(entry["id"]).lower() for marker in _NON_CHAT_MARKERS)
        ]
        return self._discovered

    def candidate_models(self) -> list[str]:
        """Configured model first, then what the account actually has, then last-resort names."""
        ordered = [settings.groq_model, *self.discover_models(), *GROQ_MODEL_FALLBACKS]
        return list(dict.fromkeys(model for model in ordered if model and model not in self._gated))

    def generate(self, policy: MerchantPolicy, intent: BuyerIntent, round_number: int, feedback: str) -> ProviderOutcome:
        if not settings.groq_api_key:
            self.last_error = "GROQ_API_KEY is not set"
            return ProviderOutcome(None, self.name, self.last_error)

        brief = _public_brief(policy, intent, round_number, feedback)
        headers = self._headers()
        errors: list[str] = []
        if self.discovery_error:
            errors.append(f"model discovery: {self.discovery_error}")

        for model in self.candidate_models():
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(brief)},
                ],
                "response_format": {"type": "json_object"},
                # Real variation between rounds and between runs comes from here. It is safe
                # because DealGuard independently re-validates whatever comes back.
                "temperature": settings.llm_temperature,
                "top_p": 0.95,
                "max_tokens": _MAX_COMPLETION_TOKENS,
            }
            try:
                output = _post_json(
                    f"{GROQ_BASE_URL}/chat/completions", payload, headers, timeout=settings.llm_timeout_seconds
                )
                content = output["choices"][0]["message"]["content"]
                offer = coerce_offer(content, policy, round_number)
            except Exception as exc:  # noqa: BLE001 - classified by _describe
                reason = _describe(exc)
                errors.append(f"{model}: {reason}")
                logger.warning("Groq proposal failed on model %s -> %s", model, reason)
                if "model_terms_required" in reason:
                    # Permanently unusable for this account, so never try it again this process.
                    self._gated.add(model)
                # A bad key or an exhausted quota will fail for every model; stop early.
                if isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403}:
                    break
                continue

            self.last_error = None
            self.last_model = model
            if model != settings.groq_model:
                logger.info("Groq model %s unavailable; served with %s instead", settings.groq_model, model)
            return ProviderOutcome(offer, f"{self.name} - {model}", None, model)

        self.last_error = " | ".join(errors) or "No Groq model produced a valid offer"
        return ProviderOutcome(None, self.name, self.last_error)


class OllamaProvider:
    """Local Ollama adapter. DealGuard still independently approves every output."""

    name = "Ollama (local LLM)"

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.last_model: str | None = settings.ollama_model

    def generate(self, policy: MerchantPolicy, intent: BuyerIntent, round_number: int, feedback: str) -> ProviderOutcome:
        brief = _public_brief(policy, intent, round_number, feedback)
        payload = {
            "model": settings.ollama_model,
            "prompt": f"{SYSTEM_PROMPT}\n\n{json.dumps(brief)}",
            "format": "json",
            "stream": False,
            "options": {
                "temperature": settings.llm_temperature,
                "top_p": 0.95,
                # Same truncation risk as Groq: a local reasoning model needs room for its
                # chain-of-thought before the JSON, or the response arrives unparseable.
                "num_predict": _MAX_COMPLETION_TOKENS,
            },
        }
        try:
            output = _post_json(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate", payload, {}, timeout=settings.llm_timeout_seconds
            )
            offer = coerce_offer(output["response"], policy, round_number)
        except Exception as exc:  # noqa: BLE001 - classified by _describe
            self.last_error = _describe(exc)
            logger.warning("Ollama proposal failed -> %s", self.last_error)
            return ProviderOutcome(None, self.name, self.last_error)

        self.last_error = None
        return ProviderOutcome(offer, f"{self.name} - {settings.ollama_model}", None, settings.ollama_model)


def configured_provider() -> LLMProvider | None:
    if settings.llm_provider == "ollama":
        return OllamaProvider()
    if settings.llm_provider == "groq":
        return GroqProvider()
    return None
