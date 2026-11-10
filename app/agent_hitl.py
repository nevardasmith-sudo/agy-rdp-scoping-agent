"""HITL Gateway: Solution Curator & Stateful Session Memory Setup (app/agent_hitl.py)."""

import os
from typing import Any, Optional

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    class BaseModel:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    def Field(default: Any = None, **kwargs: Any) -> Any:  # type: ignore
        return default

try:
    from google.adk.agents import Agent
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models import Gemini
    from google.adk.tools.tool_context import ToolContext
    from google.genai import types
except ImportError:  # pragma: no cover
    class Agent:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class CallbackContext:  # type: ignore
        pass

    class ToolContext:  # type: ignore
        pass

    class Gemini:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class _TypesStub:
        class HttpRetryOptions:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)

    types = _TypesStub()  # type: ignore

try:
    from google.cloud import firestore
except ImportError:  # pragma: no cover
    firestore = None  # type: ignore

from .telemetry import (
    emit_audit_log,
    scrub_pii_payload,
    scrub_session_and_memory_state,
    start_rpd_span,
    telemetry_after_tool_callback,
    telemetry_before_tool_callback,
    telemetry_on_model_error_callback,
)
from .tools.matcher import UseCaseCandidate

# In-memory fallback store for local/offline HITL session checkpoints
_LOCAL_HITL_SESSION_STORE: dict[str, dict[str, Any]] = {}


class ApprovedUseCases(BaseModel):
    """Output contract for the HITL Solution Curator Gateway."""

    customer_name: str = Field(
        default="", description="Name of the customer organization"
    )
    industry_vertical: str = Field(
        default="", description="Primary industry vertical"
    )
    hitl_confirmed: bool = Field(
        default=True,
        description="True only after the AI Transformation Manager explicitly approves the shortlist",
    )
    manager_notes: str = Field(
        default="",
        description="Executive notes or scoping refinements supplied by the AI Transformation Manager",
    )
    approved_use_cases: list[UseCaseCandidate] = Field(
        description="Manager-approved (checked) use cases to pass to Agent 3 (Value Driver & KPI Engine)"
    )


async def add_session_to_memory_callback(callback_context: CallbackContext) -> None:
    """Scrubs PII from session state/events and triggers asynchronous extraction into Agent Memory Bank."""
    scrub_session_and_memory_state(callback_context)
    if hasattr(callback_context, "add_session_to_memory"):
        await callback_context.add_session_to_memory()
    return None


def _resolve_session_id(
    session_id: str = "default_rpd_session",
    tool_context: Optional[ToolContext] = None,
) -> str:
    """Resolves a per-session isolated identifier from ADK `ToolContext` to prevent cross-user collisions."""
    if session_id and session_id != "default_rpd_session":
        return str(session_id).strip()
    if tool_context is not None:
        if hasattr(tool_context, "state") and tool_context.state.get("session_id"):
            return str(tool_context.state["session_id"])
        inv_ctx = getattr(tool_context, "_invocation_context", None)
        session_obj = getattr(inv_ctx, "session", None)
        if session_obj is not None and getattr(session_obj, "id", None):
            return str(session_obj.id)
        if getattr(tool_context, "invocation_id", None):
            return str(tool_context.invocation_id)
    return session_id or "default_rpd_session"


def _persist_hitl_checkpoint(session_id: str, payload: dict[str, Any]) -> str:
    """Scrubs PII and persists HITL selection state to Firestore (when configured) and local session store."""
    clean_payload, redaction_count = scrub_pii_payload(payload)
    if redaction_count > 0:
        emit_audit_log(
            event_type="PII_REDACTED",
            severity="INFO",
            message=f"Scrubbed {redaction_count} PII/secret occurrence(s) from HITL checkpoint before persistence.",
            pipeline_stage="hitl_checkpoint",
            session_id=session_id,
            redactions_applied=redaction_count,
        )
    _LOCAL_HITL_SESSION_STORE[session_id] = clean_payload
    project_id = os.environ.get(
        "RPD_GOOGLE_CLOUD_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    if firestore is not None and project_id and os.environ.get("RPD_ENABLE_FIRESTORE_HITL") == "true":
        try:
            client = firestore.Client(project=project_id)
            client.collection("rpd_hitl_checkpoints").document(session_id).set(clean_payload)
            return "firestore"
        except Exception:
            return "in_memory_fallback"
    return "in_memory"


def present_candidates_for_review(
    candidates: list[dict[str, Any]],
    customer_name: str = "",
    industry_vertical: str = "",
    session_id: str = "default_rpd_session",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Presents `RankedUseCasePool` candidates to the AI Transformation Manager and pauses for confirmation.

    Args:
        candidates: Ranked list of verified use case candidate dictionaries from Agent 2.
        customer_name: Name of the target customer organization.
        industry_vertical: Primary industry vertical of the target customer.
        session_id: Unique session identifier for HITL state persistence.
        tool_context: ADK ToolContext automatically injected by the runtime for session-isolated state.

    Returns:
        Dictionary containing `checkpoint_status`, `storage_backend`, `session_id`, `message`,
        and `candidates_for_review`.
    """
    resolved_session_id = _resolve_session_id(session_id, tool_context)
    if tool_context is not None and hasattr(tool_context, "state"):
        tool_context.state["session_id"] = resolved_session_id
        tool_context.state["hitl_status"] = "PAUSED_FOR_HITL_REVIEW"
        tool_context.state["hitl_confirmed"] = False
        tool_context.state["candidate_pool"] = list(candidates or [])
        tool_context.state["customer_name"] = customer_name
        tool_context.state["industry_vertical"] = industry_vertical

    checkpoint = {
        "session_id": resolved_session_id,
        "status": "AWAITING_MANAGER_APPROVAL",
        "customer_name": customer_name,
        "industry_vertical": industry_vertical,
        "candidate_count": len(candidates or []),
        "candidates": list(candidates or []),
    }
    storage_backend = _persist_hitl_checkpoint(resolved_session_id, checkpoint)
    with start_rpd_span(
        "rpd.present_candidates_for_review",
        {
            "rpd.session_id": resolved_session_id,
            "rpd.customer_name": customer_name,
            "rpd.candidate_count": len(candidates or []),
        },
    ):
        emit_audit_log(
            event_type="HITL_DECISION",
            severity="INFO",
            message=f"Paused at HITL gate with {len(candidates or [])} candidate(s) for review.",
            hitl_stage="PAUSED_FOR_HITL_REVIEW",
            session_id=resolved_session_id,
            customer_name=customer_name,
            candidate_count=len(candidates or []),
        )
    return {
        "checkpoint_status": "PAUSED_FOR_HITL_REVIEW",
        "storage_backend": storage_backend,
        "session_id": resolved_session_id,
        "message": (
            "Execution paused at HITL Selection Gate. Review the ranked use case candidates "
            "(with relevance scores, customer references, demo links, and capabilities sheet links) "
            "and confirm the selected use cases to proceed to Agent 3 Value & KPI modeling."
        ),
        "candidates_for_review": list(candidates or []),
    }


def submit_approved_use_cases(
    selected_use_case_names: Optional[list[str]] = None,
    candidates: Optional[list[dict[str, Any]]] = None,
    manager_notes: str = "",
    customer_name: str = "",
    industry_vertical: str = "",
    session_id: str = "default_rpd_session",
    approved_use_case_names: Optional[list[str]] = None,
    candidate_pool: Optional[list[dict[str, Any]]] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Records manager-approved use cases (`ApprovedUseCases`) and removes unchecked items from downstream compute.

    Args:
        selected_use_case_names: List of use case titles explicitly approved by the AI Transformation Manager.
        candidates: Candidate pool dictionaries to filter against `selected_use_case_names`.
        manager_notes: Optional executive scoping notes supplied by the AI Transformation Manager.
        customer_name: Name of the target customer organization.
        industry_vertical: Primary industry vertical of the target customer.
        session_id: Unique session identifier for HITL state persistence.
        approved_use_case_names: Alias for `selected_use_case_names`.
        candidate_pool: Alias for `candidates`.
        tool_context: ADK ToolContext automatically injected by the runtime for session-isolated state.

    Returns:
        Dictionary conforming to `ApprovedUseCases` with `hitl_status`, `hitl_confirmed`,
        `approved_count`, and `approved_use_cases`.
    """
    resolved_session_id = _resolve_session_id(session_id, tool_context)
    names_to_use = (
        selected_use_case_names
        if selected_use_case_names is not None
        else (approved_use_case_names or [])
    )
    pool_to_use = candidates if candidates is not None else candidate_pool

    if pool_to_use is None and tool_context is not None and hasattr(tool_context, "state"):
        pool_to_use = tool_context.state.get("candidate_pool")
        customer_name = customer_name or str(tool_context.state.get("customer_name") or "")
        industry_vertical = industry_vertical or str(tool_context.state.get("industry_vertical") or "")

    if pool_to_use is None:
        stored = _LOCAL_HITL_SESSION_STORE.get(resolved_session_id, {})
        pool_to_use = stored.get("candidates", [])
        customer_name = customer_name or stored.get("customer_name", "")
        industry_vertical = industry_vertical or stored.get("industry_vertical", "")

    selected_lower = {
        str(name).strip().lower() for name in names_to_use if str(name).strip()
    }
    approved_dicts = [
        c
        for c in (pool_to_use or [])
        if str(c.get("use_case_name", "")).strip().lower() in selected_lower
    ]

    approved_models = [
        UseCaseCandidate(
            use_case_name=str(c.get("use_case_name") or ""),
            use_case_description=str(c.get("use_case_description") or ""),
            industry_domain=str(c.get("industry_domain") or ""),
            sub_industry=str(c.get("sub_industry") or ""),
            status=str(c.get("status") or "Deployed - Field Ready"),
            customer_reference=str(c.get("customer_reference") or ""),
            demo_link=str(c.get("demo_link") or ""),
            capabilities_sheet_link=str(c.get("capabilities_sheet_link") or ""),
            business_value=str(c.get("business_value") or ""),
            kpis=str(c.get("kpis") or ""),
            score=int(c.get("score") or 0),
        )
        for c in approved_dicts
    ]
    validated_approval = ApprovedUseCases(
        customer_name=customer_name,
        industry_vertical=industry_vertical,
        hitl_confirmed=True,
        manager_notes=manager_notes,
        approved_use_cases=approved_models,
    )
    dumped_approved = [
        m.model_dump() if hasattr(m, "model_dump") else m.__dict__
        for m in validated_approval.approved_use_cases
    ]

    result = {
        "session_id": resolved_session_id,
        "customer_name": validated_approval.customer_name,
        "industry_vertical": validated_approval.industry_vertical,
        "hitl_status": "APPROVED",
        "hitl_confirmed": validated_approval.hitl_confirmed,
        "manager_notes": validated_approval.manager_notes,
        "approved_count": len(dumped_approved),
        "approved_use_cases": dumped_approved,
    }
    if tool_context is not None and hasattr(tool_context, "state"):
        tool_context.state["session_id"] = resolved_session_id
        tool_context.state["hitl_status"] = "APPROVED"
        tool_context.state["hitl_confirmed"] = True
        tool_context.state["approved_use_cases"] = dumped_approved

    _persist_hitl_checkpoint(
        resolved_session_id,
        {
            "session_id": resolved_session_id,
            "status": "APPROVED",
            **result,
        },
    )
    with start_rpd_span(
        "rpd.submit_approved_use_cases",
        {
            "rpd.session_id": resolved_session_id,
            "rpd.approved_count": len(dumped_approved),
        },
    ):
        emit_audit_log(
            event_type="HITL_DECISION",
            severity="INFO",
            message=f"Manager approved {len(dumped_approved)} use case(s) at HITL gate.",
            hitl_stage="APPROVED",
            session_id=resolved_session_id,
            approved_count=len(dumped_approved),
            approved_use_case_names=[c["use_case_name"] for c in dumped_approved],
        )
    return result


from .config import build_flash_model, build_gemini_model, enforce_hitl_gate_before_tool
from .telemetry import (
    strategic_model_router_callback,
    telemetry_after_model_callback,
)

hitl_curator_agent = Agent(
    name="solution_curator_hitl",
    model=build_flash_model(),
    instruction=(
        "You are the HITL Gateway: Solution Curator for the Google Cloud Repeatable, Portable, "
        "and Demoable (RPD) Transformation Engine.\n"
        "1. Present the `RankedUseCasePool` candidate list (including relevance scores, customer "
        "references, demo links, and business capabilities/architecture links) to the AI "
        "Transformation Manager using `present_candidates_for_review`.\n"
        "2. MANDATORY GOVERNANCE GATE: Pause execution and wait until the AI Transformation "
        "Manager explicitly reviews, refines, and confirms their selected target use cases.\n"
        "3. Never proceed to value/KPI modeling without explicit human confirmation.\n"
        "4. Once the manager confirms their selections (and any optional executive notes), invoke "
        "`submit_approved_use_cases` so only the manager-approved use cases (`ApprovedUseCases`) "
        "are transmitted downstream to Agent 3."
    ),
    output_key="approved_use_cases",
    before_model_callback=[strategic_model_router_callback],
    after_model_callback=[telemetry_after_model_callback],
    before_tool_callback=[enforce_hitl_gate_before_tool, telemetry_before_tool_callback],
    after_tool_callback=[telemetry_after_tool_callback],
    on_model_error_callback=[telemetry_on_model_error_callback],
    after_agent_callback=[add_session_to_memory_callback],
    tools=[present_candidates_for_review, submit_approved_use_cases],
)

hitl_agent = hitl_curator_agent

__all__ = [
    "ApprovedUseCases",
    "add_session_to_memory_callback",
    "present_candidates_for_review",
    "submit_approved_use_cases",
    "hitl_curator_agent",
    "hitl_agent",
]
