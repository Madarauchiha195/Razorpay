from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from ..domain.models import AuthorizedDeal


def _normalise(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _normalise(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


def canonical_deal_payload(deal: AuthorizedDeal) -> bytes:
    """Stable payload excluding the signature itself, for tamper detection."""
    body = deal.model_dump(mode="python", exclude={"signature"})
    return json.dumps(_normalise(body), sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_deal(deal: AuthorizedDeal, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_deal_payload(deal), hashlib.sha256).hexdigest()


def verify_deal_signature(deal: AuthorizedDeal, secret: str) -> bool:
    expected = sign_deal(deal, secret)
    return hmac.compare_digest(expected, deal.signature)

