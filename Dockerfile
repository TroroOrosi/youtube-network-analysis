# Hosted deployment of web_ui. Test-only dependencies live in
# web_ui/requirements-dev.txt and never enter this image.
FROM python:3.14-slim

WORKDIR /app

COPY web_ui/requirements.txt web_ui/requirements.txt
RUN pip install --no-cache-dir -r web_ui/requirements.txt

COPY web_ui/ web_ui/
COPY analysis_api/ analysis_api/
COPY channel_connections/ channel_connections/
COPY channel_data/ channel_data/
COPY collection_jobs/ collection_jobs/
COPY workspace_access/ workspace_access/
# Only analytics_core is imported from this package. The rest of it pulls pandas
# and the Google API client, which this layer never uses.
COPY subscriber_analytics/analytics_core.py subscriber_analytics/analytics_core.py

USER nobody

# The platform sets $PORT. `--proxy-headers` is required because TLS terminates
# in front of this process; the proxy's address goes in FORWARDED_ALLOW_IPS,
# which uvicorn reads on its own, so the rate limiter sees real clients.
#
# `--no-access-log` because the host already logs every request with its method,
# path, status and latency, and uvicorn's copy adds one thing to that: a second
# recording of the OAuth `code` that arrives in the query string of both
# callbacks. The code is single-use, short-lived and bound to a PKCE verifier,
# so the copy is not worth keeping. Errors and tracebacks still go to stdout;
# only the per-request access line is dropped.
ENV PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["sh", "-c", "exec python -m uvicorn web_ui.main:app --host 0.0.0.0 --port \"${PORT}\" --workers 1 --proxy-headers --no-access-log"]
