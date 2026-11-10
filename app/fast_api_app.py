"""Production FastAPI Entrypoint with OpenTelemetry & Health Probes (app/fast_api_app.py)."""

import os
from typing import Any

from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

from .telemetry import AUDIT_LOG_BUFFER, configure_opentelemetry
from .tools.matcher import DEFAULT_DB_PATH

configure_opentelemetry(service_name="agy-rpd-scoping-agent")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("RPD_ALLOW_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if origin.strip()
]

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    allow_origins=ALLOW_ORIGINS,
    web=True,
)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Production liveness and repository readiness probe."""
    db_exists = os.path.exists(DEFAULT_DB_PATH)
    return {
        "status": "ok" if db_exists else "degraded",
        "service": "agy-rpd-scoping-agent",
        "repository_db_ready": db_exists,
        "audit_events_logged": len(AUDIT_LOG_BUFFER),
    }
