"""OpenTelemetry Tracing & Google Cloud Structured JSON Audit Logging (app/telemetry.py)."""

import datetime
import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    trace = None  # type: ignore
    Resource = None  # type: ignore
    TracerProvider = None  # type: ignore
    SimpleSpanProcessor = None  # type: ignore
    InMemorySpanExporter = None  # type: ignore
    _OTEL_AVAILABLE = False

try:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext
except ImportError:  # pragma: no cover
    class CallbackContext:  # type: ignore
        pass

    class BaseTool:  # type: ignore
        name: str = ""

    class ToolContext:  # type: ignore
        pass


_audit_logger = logging.getLogger("rpd.scoping.audit")
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _audit_logger.addHandler(_handler)
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False

# Ring buffer of recent structured audit events for deterministic verification and diagnostics
AUDIT_LOG_BUFFER: list[dict[str, Any]] = []
SPAN_EXPORTER: Any = None
_TRACER_INITIALIZED = False


def configure_opentelemetry(service_name: str = "agy-rpd-scoping-agent") -> Any:
    """Initializes the OpenTelemetry TracerProvider and in-memory / Cloud Trace span processors."""
    global _TRACER_INITIALIZED, SPAN_EXPORTER
    if not _OTEL_AVAILABLE or trace is None:
        return None

    if not _TRACER_INITIALIZED:
        current_provider = trace.get_tracer_provider()
        if TracerProvider is not None and not isinstance(current_provider, TracerProvider):
            resource = Resource.create(
                {
                    "service.name": service_name,
                    "service.namespace": "google-cloud-consulting-rpd",
                    "gcp.project_id": os.environ.get(
                        "RPD_GOOGLE_CLOUD_PROJECT",
                        os.environ.get("GOOGLE_CLOUD_PROJECT", "nevardaai5days-sandbox-232845"),
                    ),
                }
            )
            provider = TracerProvider(resource=resource)
            if InMemorySpanExporter is not None and SimpleSpanProcessor is not None:
                SPAN_EXPORTER = InMemorySpanExporter()
                provider.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
            trace.set_tracer_provider(provider)
        elif (
            TracerProvider is not None
            and isinstance(current_provider, TracerProvider)
            and SPAN_EXPORTER is None
            and InMemorySpanExporter is not None
            and SimpleSpanProcessor is not None
        ):
            SPAN_EXPORTER = InMemorySpanExporter()
            current_provider.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
        _TRACER_INITIALIZED = True

    return trace.get_tracer(service_name)


class _NoOpSpan:
    """Fallback span object when OpenTelemetry is unavailable."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[dict[str, Any]] = None) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass


@contextmanager
def start_rpd_span(
    span_name: str, attributes: Optional[dict[str, Any]] = None
) -> Iterator[Any]:
    """Starts an OpenTelemetry span and attaches RPD domain attributes."""
    tracer = configure_opentelemetry()
    if tracer is None:
        span = _NoOpSpan()
        yield span
        return

    with tracer.start_as_current_span(span_name) as span:
        if attributes:
            for k, v in attributes.items():
                if v is not None:
                    span.set_attribute(k, v)
        yield span


def _current_trace_id_hex() -> str:
    """Returns the current OpenTelemetry 32-char hex trace ID if active."""
    if not _OTEL_AVAILABLE or trace is None:
        return ""
    try:
        current_span = trace.get_current_span()
        ctx = current_span.get_span_context() if current_span else None
        if ctx and getattr(ctx, "is_valid", False):
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return ""


def emit_audit_log(
    event_type: str,
    severity: str = "INFO",
    message: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """Emits a Google Cloud Structured JSON log entry for governance and HITL auditing."""
    project_id = os.environ.get(
        "RPD_GOOGLE_CLOUD_PROJECT",
        os.environ.get("GOOGLE_CLOUD_PROJECT", "nevardaai5days-sandbox-232845"),
    )
    trace_hex = _current_trace_id_hex()
    entry: dict[str, Any] = {
        "severity": severity.upper(),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rpd_event_type": event_type,
        "message": message or f"RPD governance event: {event_type}",
        **fields,
    }
    if trace_hex:
        entry["logging.googleapis.com/trace"] = f"projects/{project_id}/traces/{trace_hex}"

    AUDIT_LOG_BUFFER.append(entry)
    if len(AUDIT_LOG_BUFFER) > 500:
        del AUDIT_LOG_BUFFER[: len(AUDIT_LOG_BUFFER) - 500]

    _audit_logger.info(json.dumps(entry, default=str))
    return entry


def telemetry_before_tool_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> Optional[dict[str, Any]]:
    """ADK `before_tool_callback` that records tool start time and emits a structured invocation log."""
    tool_name = getattr(tool, "name", "") or str(tool)
    state = getattr(tool_context, "state", None)
    if state is not None:
        state[f"_telemetry_start_{tool_name}"] = time.perf_counter()

    emit_audit_log(
        event_type="TOOL_INVOCATION_START",
        severity="INFO",
        message=f"Starting RPD tool `{tool_name}`",
        tool_name=tool_name,
        invocation_id=str(getattr(tool_context, "invocation_id", "") or ""),
        arg_keys=sorted(list((args or {}).keys())),
    )
    return None


def telemetry_after_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """ADK `after_tool_callback` that records tool execution latency and outcome metrics."""
    tool_name = getattr(tool, "name", "") or str(tool)
    state = getattr(tool_context, "state", None)
    duration_ms = 0.0
    if state is not None:
        start_ts = state.get(f"_telemetry_start_{tool_name}")
        if isinstance(start_ts, (int, float)):
            duration_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)

    is_error = isinstance(tool_response, dict) and bool(tool_response.get("error"))
    emit_audit_log(
        event_type="TOOL_INVOCATION_COMPLETE",
        severity="WARNING" if is_error else "INFO",
        message=f"Completed RPD tool `{tool_name}` in {duration_ms} ms",
        tool_name=tool_name,
        duration_ms=duration_ms,
        status="ERROR" if is_error else "OK",
        invocation_id=str(getattr(tool_context, "invocation_id", "") or ""),
    )
    return None


def telemetry_on_model_error_callback(
    callback_context: CallbackContext, llm_request: Any, error: Exception
) -> Optional[Any]:
    """ADK `on_model_error_callback` that records structured telemetry on LLM generation errors."""
    emit_audit_log(
        event_type="LLM_MODEL_ERROR",
        severity="ERROR",
        message=f"Model invocation error: {str(error)}",
        error_type=type(error).__name__,
        error_detail=str(error),
        agent_name=str(getattr(callback_context, "agent_name", "") or ""),
        invocation_id=str(getattr(callback_context, "invocation_id", "") or ""),
    )
    return None


def strategic_model_router_callback(
    callback_context: CallbackContext, llm_request: Any
) -> Optional[Any]:
    """ADK `before_model_callback` implementing Strategic Model Routing between Flash and Pro tiers.

    Routing Logic:
    - Delegates complex reasoning, Zero-Hallucination KPI verification (`value_driver_kpi_engine`),
      and Executive Synthesis (`executive_opportunity_synthesizer`, or post-HITL confirmed turns on
      `rpd_transformation_synthesizer`) to the **Pro** model tier (`PRO_MODEL_NAME`).
    - Routes high-throughput ingestion, SQLite repository matching, and pre-HITL candidate presentation
      to the **Flash** model tier (`FLASH_MODEL_NAME`) for low latency and FinOps efficiency.
    - Emits an OpenTelemetry span (`rpd.strategic_model_router`) and a `MODEL_ROUTING_DECISION` audit log.
    """
    from .config import AGENT_MODEL_ROUTING_TABLE, FLASH_MODEL_NAME, PRO_MODEL_NAME

    agent_name = str(getattr(callback_context, "agent_name", "") or "rpd_transformation_synthesizer")
    state = getattr(callback_context, "state", None)
    hitl_confirmed = bool(state.get("hitl_confirmed")) if state is not None else False

    # Inspect request contents for complex synthesis/value keywords when on root_agent
    request_text = str(getattr(llm_request, "contents", "") or "").lower()
    is_complex_synthesis_turn = (
        agent_name in ("value_driver_kpi_engine", "executive_opportunity_synthesizer")
        or hitl_confirmed
        or any(
            kw in request_text
            for kw in (
                "synthesize_transformation_report",
                "extract_verified_value_and_kpis",
                "executive report",
                "transformation opportunity for",
                "value dossier",
            )
        )
    )

    if is_complex_synthesis_turn:
        selected_model = PRO_MODEL_NAME
        routing_tier = "PRO_COMPLEX_REASONING"
        routing_reason = (
            f"Delegated `{agent_name}` to Pro tier (`{PRO_MODEL_NAME}`) for Zero-Hallucination KPI "
            "verification and executive opportunity synthesis."
        )
    else:
        route_entry = AGENT_MODEL_ROUTING_TABLE.get(agent_name, {})
        selected_model = route_entry.get("model", FLASH_MODEL_NAME)
        if agent_name == "rpd_transformation_synthesizer":
            selected_model = FLASH_MODEL_NAME
        routing_tier = "FLASH_LOW_LATENCY"
        routing_reason = (
            f"Routed `{agent_name}` to Flash tier (`{selected_model}`) for low-latency discovery, "
            "deterministic SQLite matching, and HITL candidate presentation."
        )

    if llm_request is not None and hasattr(llm_request, "model"):
        llm_request.model = selected_model

    if state is not None:
        state["active_model_tier"] = routing_tier
        state["active_model_name"] = selected_model

    with start_rpd_span(
        "rpd.strategic_model_router",
        {
            "rpd.model_routing.agent_name": agent_name,
            "rpd.model_routing.tier": routing_tier,
            "rpd.model_routing.selected_model": selected_model,
            "rpd.model_routing.hitl_confirmed": hitl_confirmed,
        },
    ):
        emit_audit_log(
            event_type="MODEL_ROUTING_DECISION",
            severity="INFO",
            message=routing_reason,
            agent_name=agent_name,
            model_tier=routing_tier,
            selected_model=selected_model,
            hitl_confirmed=hitl_confirmed,
            invocation_id=str(getattr(callback_context, "invocation_id", "") or ""),
        )
    return None


def telemetry_after_model_callback(
    callback_context: CallbackContext, llm_response: Any
) -> Optional[Any]:
    """ADK `after_model_callback` recording token consumption by strategic model tier."""
    state = getattr(callback_context, "state", None)
    model_tier = (
        str(state.get("active_model_tier") or "UNKNOWN")
        if state is not None
        else "UNKNOWN"
    )
    selected_model = (
        str(state.get("active_model_name") or "")
        if state is not None
        else ""
    )
    usage = getattr(llm_response, "usage_metadata", None)
    prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    candidate_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
    total_tokens = int(getattr(usage, "total_token_count", 0) or 0) if usage else 0

    emit_audit_log(
        event_type="LLM_TOKEN_USAGE",
        severity="INFO",
        message=f"Model `{selected_model}` ({model_tier}) consumed {total_tokens} total tokens.",
        agent_name=str(getattr(callback_context, "agent_name", "") or ""),
        model_tier=model_tier,
        selected_model=selected_model,
        prompt_token_count=prompt_tokens,
        candidates_token_count=candidate_tokens,
        total_token_count=total_tokens,
        invocation_id=str(getattr(callback_context, "invocation_id", "") or ""),
    )
    return None
