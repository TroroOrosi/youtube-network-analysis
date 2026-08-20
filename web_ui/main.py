"""ASGI entry point: `python -m uvicorn web_ui.main:app`."""

from __future__ import annotations

import os

from .app import create_app

BASE_URL = os.environ.get("YNA_BASE_URL", "https://localhost:8000")

app = create_app(base_url=BASE_URL)
