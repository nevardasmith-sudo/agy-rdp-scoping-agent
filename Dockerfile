# syntax=docker/dockerfile:1
# Stage 1: Dependency Builder with astral-sh/uv
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY app ./app

RUN uv sync --frozen --no-dev --no-editable

# Stage 2: Hardened Non-Root Runtime Image
FROM python:3.11-slim-bookworm AS runtime

RUN groupadd --gid 10001 appuser && \
    useradd --uid 10001 --gid appuser --shell /sbin/nologin --create-home appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser app /app/app
COPY --chown=appuser:appuser agents-cli-manifest.yaml eval_config.json rpd_eval_set.json test_pipeline.py /app/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    RPD_SQLITE_DB_PATH="/app/app/data/fsi_use_cases.db" \
    GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY="true" \
    OTEL_SEMCONV_STABILITY_OPT_IN="gen_ai_latest_experimental"

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
