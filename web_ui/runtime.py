"""Production startup validation and non-sensitive operational diagnostics.

Configuration validation is standard-library only so Cloud Shell can use the
same rules before touching a deployment. Probes never call Google or a store.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import logging
import os
import re
import secrets
from urllib.parse import urlsplit

_LOG = logging.getLogger(__name__)


def validate_production_environment(environment: Mapping[str, str]) -> None:
    """Reject accidental public demo/ephemeral operation; never echo values."""
    if not environment.get('K_SERVICE') and environment.get('YNA_ENVIRONMENT') != 'production':
        return
    required = (
        'YNA_BASE_URL', 'YNA_REQUIRE_GOOGLE', 'YNA_GOOGLE_CLIENT_ID',
        'YNA_GOOGLE_CLIENT_SECRET', 'YNA_FIRESTORE_DATABASE',
        'YNA_CREDENTIAL_SECRET', 'YNA_YOUTUBE_API_KEY', 'YNA_DRAIN_SERVICE_ACCOUNT',
    )
    errors = [name for name in required if not environment.get(name, '').strip()]
    base = environment.get('YNA_BASE_URL', '').strip()
    try:
        url = urlsplit(base)
        valid_origin = (
            url.scheme == 'https' and bool(url.hostname)
            and url.hostname not in ('localhost', '127.0.0.1', '::1')
            and url.username is None and url.password is None
            and url.port in (None, 443) and url.path in ('', '/')
            and not url.query and not url.fragment
            and not any(char.isspace() for char in base)
        )
    except ValueError:
        valid_origin = False
    if not valid_origin:
        errors.append('YNA_BASE_URL')
    if environment.get('YNA_REQUIRE_GOOGLE', '').strip() != '1':
        errors.append('YNA_REQUIRE_GOOGLE')
    resource_patterns = {
        'YNA_FIRESTORE_DATABASE': r'projects/[A-Za-z0-9_-]+/databases/(?:\(default\)|[A-Za-z0-9_-]+)',
        'YNA_CREDENTIAL_SECRET': r'projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+',
        'YNA_DRAIN_SERVICE_ACCOUNT': r'[A-Za-z0-9_.-]+@[A-Za-z0-9_-]+\.iam\.gserviceaccount\.com',
    }
    for name, pattern in resource_patterns.items():
        if not re.fullmatch(pattern, environment.get(name, '').strip()):
            errors.append(name)
    if environment.get('YNA_STATE_DIR', '').strip():
        errors.append('YNA_STATE_DIR (ephemeral storage is forbidden on Cloud Run)')
    if environment.get('WEB_CONCURRENCY', '').strip() not in ('', '1'):
        errors.append('WEB_CONCURRENCY (one worker is required)')
    if errors:
        raise RuntimeError('Unsafe production configuration: ' + ', '.join(sorted(set(errors))))


def install_operational_routes(app) -> None:
    """Attach cheap probes and request IDs after services initialized.

    Readiness means the application loaded its configuration and snapshots. It
    deliberately does NOT assert current OAuth, quota, Firestore, or YouTube
    health. Those need the authenticated acceptance test in the runbook.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from .build_info import SOURCE_REVISION

    revision = os.environ.get('K_REVISION', 'local')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', revision):
        revision = 'unknown'
    source = SOURCE_REVISION if re.fullmatch(r'[a-f0-9]{40}', SOURCE_REVISION) else 'unknown'
    mode = 'google' if app.state.services.login is not None else 'demo'

    @app.get('/health/live', include_in_schema=False)
    async def live():
        return JSONResponse({'status': 'live'})

    @app.get('/health/ready', include_in_schema=False)
    async def ready():
        return JSONResponse({
            'status': 'ready', 'scope': 'application_initialization',
            'mode': mode, 'revision': revision, 'source_revision': source,
        })

    @app.middleware('http')
    async def diagnostics(request: Request, call_next):
        request_id = secrets.token_hex(16)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers['X-YNA-Request-ID'] = request_id
        response.headers['X-YNA-Revision'] = revision
        response.headers['X-YNA-Source'] = source
        if response.status_code >= 400:
            # Route templates are code, unlike URLs, queries, headers and form
            # data. Never log exception messages, session IDs or provider bodies.
            route = request.scope.get('route')
            code = getattr(request.state, 'error_code', 'HTTP_ERROR')
            if not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9_]{1,80}', code):
                code = 'HTTP_ERROR'
            _LOG.warning(json.dumps({
                'event': 'request_rejected', 'request_id': request_id,
                'revision': revision, 'source_revision': source,
                'route': getattr(route, 'path', 'unmatched'),
                'status': response.status_code, 'code': code,
            }, separators=(',', ':')))
        return response
