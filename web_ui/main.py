"""ASGI entry point: `python -m uvicorn web_ui.main:app`."""

from __future__ import annotations

import os

from .runtime import validate_production_environment

# Refuse incomplete hosted settings before constructing any service.
validate_production_environment(os.environ)

from .app import create_app
from .container import (
    build_services,
    drain_caller_from_env,
    durability_from_env,
    google_config_from_env,
    youtube_api_key_from_env,
)

BASE_URL = os.environ.get("YNA_BASE_URL", "https://localhost:8000")

app = create_app(
    build_services(
        BASE_URL,
        google=google_config_from_env(os.environ, BASE_URL),
        durability=durability_from_env(os.environ),
        youtube_api_key=youtube_api_key_from_env(os.environ),
        drain_caller=drain_caller_from_env(os.environ, BASE_URL),
    )
)
