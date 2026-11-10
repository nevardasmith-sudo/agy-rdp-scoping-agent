"""OpenTelemetry Tracing, PII Scrubbing, & Google Cloud Structured JSON Audit Logging (app/telemetry.py)."""

import datetime
import json
import logging
import os
import re
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


# Deterministic PII & Secret Redaction Patterns
PII_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Google API Keys, Vertex Keys, GitHub PATs, and Bearer Tokens
    (
        re.compile(
            r"\b(?:AQ\.[A-Za-z0-9_\-]{20,}|AIza[0-9A-Za-z_\-]{35}|github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,})\b"
        ),
        "[REDACTED_SECRET]",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\._\-]{16,}\b"),
        "Bearer [REDACTED_SECRET]",
    ),
    # US Social Security Numbers (SSN: XXX-XX-XXXX)
    (
        re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    # Payment / Credit Card Numbers (13-16 digits with spaces or dashes)
    (
        re.compile(r"\b(?:\d{4}[-\s]){3}\d{4}\b|\b\d{15,16}\b"),
        "[REDACTED_CREDIT_CARD]",
    ),
    # Email Addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    # North American & International Phone Numbers
    (
        re.compile(
            r"(?<!\d)(?:\+?1[\s.\-]?)?\(?\d{3}\)[\s.\-]?\d{3}[\s.\-]\d{4}(?!\d)|(?<!\d)(?:\+?1[\s.\-])?\d{3}[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)"
        ),
        "[REDACTED_PHONE]",
    ),
]

SENSITIVE_KEY_NAMES = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "ssn",
    "social_security_number",
    "credit_card",
    "card_number",
    "tax_id",
}


def scrub_pii_with_count(text: str) -> tuple[str, int]:
    """Scrubs PII and secrets from a string, returning `(scrubbed_text, total_redactions)`."""
    if not text or not isinstance(text, str):
        return text, 0
    scrubbed = text
    total_count = 0
    for pattern, replacement in PII_REDACTION_PATTERNS:
        scrubbed, count = pattern.subn(replacement, scrubbed)
        total_count += count
    return scrubbed, total_count


def scrub_pii(text: str) -> str:
    """Scrubs PII and secrets from a string."""
    scrubbed, _ = scrub_pii_with_count(text)
    return scrubbed


def scrub_pii_payload(payload: Any) -> tuple[Any, int]:
    """Recursively scrubs PII and sensitive keys across dicts, lists, tuples, and strings."""
    if isinstance(payload, str):
        return scrub_pii_with_count(payload)
    if isinstance(payload, dict):
        out_dict: dict[str, Any] = {}
        total_redactions = 0
        for k, v in payload.items():
            key_lower = str(k).strip().lower()
            if key_lower in SENSITIVE_KEY_NAMES and v:
                out_dict[k] = "[REDACTED_SENSITIVE_FIELD]"
                total_redactions += 1
            else:
                clean_v, count = scrub_pii_payload(v)
                out_dict[k] = clean_v
                total_redactions += count
        return out_dict, total_redactions
    if isinstance(payload, list):
        out_list: list[Any] = []
        total_redactions = 0
        for item in payload:
            clean_item, count = scrub_pii_payload(item)
            out_list.append(clean_item)
            total_redactions += count
        return out_list, total_redactions
    if isinstance(payload, tuple):
        out_items: list[Any] = []
        total_redactions = 0
        for item in payload:
            clean_item, count = scrub_pii_payload(item)
            out_items.append(clean_item)
            total_redactions += count
        return tuple(out_items), total_redactions
    return payload, 0


def scrub_session_and_memory_state(callback_context: Any) -> int:
    """Actively scrubs PII from ADK session state and conversation events prior to Memory Bank persistence."""
    total_redactions = 0
    state = getattr(callback_context, "state", None)
    if state is not None and hasattr(state, "items"):
        keys_to_update: dict[str, Any] = {}
        for k, v in list(state.items()):
            clean_v, count = scrub_pii_payload(v)
            if count > 0:
                keys_to_update[k] = clean_v
                total_redactions += count
        for k, clean_v in keys_to_update.items():
            state[k] = clean_v

    inv_ctx = getattr(callback_context, "_invocation_context", None)
    session_obj = getattr(inv_ctx, "session", None)
    events = getattr(session_obj, "events", None) or []
    for event in events:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text:
                clean_text, count = scrub_pii_with_count(part_text)
                if count > 0:
                    part.text = clean_text
                    total_redactions += count

    if total_redactions > 0:
        emit_audit_log(
            event_type="PII_REDACTED",
            severity="INFO",
            message=f"Scrubbed {total_redactions} PII/secret occurrence(s) from session memory pipeline.",
            pipeline_stage="memory_persistence",
            redactions_applied=total_redactions,
        )
    return total_redactions


class PiiRedactingFilter(logging.Filter):
    """Logging filter that guarantees no unredacted PII or secrets leave the Python logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_pii(record.msg)
        if record.args:
            clean_args, _ = scrub_pii_payload(record.args)
            record.args = clean_args
        return True


_audit_logger = logging.getLogger("rpd.scoping.audit")
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _handler.addFilter(PiiRedactingFilter())
    _audit_logger.addHandler(_handler)
_audit_logger.addFilter(PiiRedactingFilter())
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
            clean_attrs, _ = scrub_pii_payload(attributes)
            for k, v in clean_attrs.items():
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
    """Emits a PII-scrubbed Google Cloud Structured JSON log entry for governance and HITL auditing."""
    project_id = os.environ.get(
        "RPD_GOOGLE_CLOUD_PROJECT",
        os.environ.get("GOOGLE_CLOUD_PROJECT", "nevardaai5days-sandbox-232845"),
    )
    trace_hex = _current_trace_id_hex()
    raw_msg = message or f"RPD governance event: {event_type}"
    clean_msg, msg_redactions = scrub_pii_with_count(raw_msg)
    clean_fields, field_redactions = scrub_pii_payload(fields)
    total_redactions = msg_redactions + field_redactions

    entry: dict[str, Any] = {
        "severity": severity.upper(),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rpd_event_type": event_type,
        "message": clean_msg,
        "pii_scrubbed": total_redactions > 0,
        **clean_fields,
    }
    if total_redactions > 0:
        entry["pii_redactions_count"] = total_redactions
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

    # Scrub any PII from session state and incoming LLM request contents before model execution
    scrub_session_and_memory_state(callback_context)
    contents_obj = getattr(llm_request, "contents", None)
    if isinstance(contents_obj, str):
        clean_contents, redaction_cnt = scrub_pii_with_count(contents_obj)
        if redaction_cnt > 0:
            llm_request.contents = clean_contents
    elif isinstance(contents_obj, list):
        for content_item in contents_obj:
            for part in getattr(content_item, "parts", None) or []:
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str) and part_text:
                    clean_part, _ = scrub_pii_with_count(part_text)
                    part.text = clean_part

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
