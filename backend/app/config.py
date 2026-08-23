from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _resolve_provider() -> str:
    """Pick the proposal engine, preferring an explicit choice.

    If the operator sets a key but forgets LLM_PROVIDER, the app used to stay silently
    deterministic. Auto-detecting a configured key removes that failure mode. Set
    LLM_PROVIDER=deterministic explicitly to force the offline engine.
    """
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    if os.getenv("OLLAMA_MODEL") or os.getenv("OLLAMA_BASE_URL"):
        return "ollama"
    return "deterministic"


@dataclass(frozen=True)
class Settings:
    app_name: str = "DealMesh API"
    environment: str = os.getenv("ENVIRONMENT", "development")
    # A relative SQLite URL keeps local startup portable on Windows, macOS, and Linux.
    # Set DATABASE_URL to PostgreSQL in a shared environment.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./dealmesh.db")
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "").split(",")
        if origin.strip()
    )
    deal_signing_secret: str = os.getenv("DEAL_SIGNING_SECRET", "development-only-change-me")
    deal_expiry_minutes: int = int(os.getenv("DEAL_EXPIRY_MINUTES", "15"))
    llm_provider: str = _resolve_provider()
    # Above 0 so proposals genuinely differ between rounds and between runs. Safe at any
    # value because DealGuard re-derives every number server-side before authorizing.
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.8"))
    llm_timeout_seconds: int = int(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    groq_api_key: str | None = os.getenv("GROQ_API_KEY") or None
    # Apache-2.0 open-weight, on Groq's free tier, and currently in their production model list.
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    razorpay_key_id: str | None = os.getenv("RAZORPAY_KEY_ID") or None
    razorpay_key_secret: str | None = os.getenv("RAZORPAY_KEY_SECRET") or None
    razorpay_webhook_secret: str | None = os.getenv("RAZORPAY_WEBHOOK_SECRET") or None


settings = Settings()
