"""Vercel entrypoint for the DealMesh FastAPI application."""

from __future__ import annotations

import os
import sys
from pathlib import Path


backend_dir = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(backend_dir))

# Vercel's deployed filesystem is read-only. SQLite is suitable for a temporary
# demo only; use DATABASE_URL for any deployment that needs persistent data.
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/dealmesh.db")

from app.main import app  # noqa: E402


__all__ = ["app"]