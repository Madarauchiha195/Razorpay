"""Vercel entrypoint for the DealMesh FastAPI application."""

from __future__ import annotations

import os


# Vercel's deployed filesystem is read-only. SQLite is suitable for a temporary
# demo only; use DATABASE_URL for any deployment that needs persistent data.
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/dealmesh.db")

from app.main import app  # noqa: E402


__all__ = ["app"]