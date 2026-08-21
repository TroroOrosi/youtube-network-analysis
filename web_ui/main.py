"""ASGI entry point: `python -m uvicorn web_ui.main:app`."""

from __future__ import annotations

import os

from .app import create_app
from .container import (
    build_services,
    google_config_from_env,
    state_dir_from_env,
    youtube_api_key_from_env,
)

BASE_URL = os.environ.get("YNA_BASE_URL", "https://localhost:8000")

app = create_app(
    build_services(
        BASE_URL,
        google=google_config_from_env(os.environ, BASE_URL),
        state_dir=state_dir_from_env(os.environ),
        youtube_api_key=youtube_api_key_from_env(os.environ),
    )
)
