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
    from google.genai import types
except ImportError:  # pragma: no cover
    class Agent:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class CallbackContext:  # type: ignore
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
    """Triggers asynchronous background scanning of conversation history to extract persistent facts into Agent Memory Bank."""
    if hasattr(callback_context, "add_session_to_memory"):
        await callback_context.add_session_to_memory()
    return None


def _persist_hitl_checkpoint(session_id: str, payload: dict[str, Any]) -> str:
    """Persists HITL selection state to Firestore (when configured) and local session store."""
    _LOCAL_HITL_SESSION_STORE[session_id] = payload
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if firestore is not None and project_id and os.environ.get("RPD_ENABLE_FIRESTORE_HITL") == "true":
        try:
            client = firestore.Client(project=project_id)
            client.collection("rpd_hitl_checkpoints").document(session_id).set(payload)
            return "firestore"
        except Exception:
            return "in_memory_fallback"
    return "in_memory"


def present_candidates_for_review(
    candidates: list[dict[str, Any]],
    customer_name: str = "",
    industry_vertical: str = "",
    session_id: str = "default_rpd_session",
) -> dict[str, Any]:
    """Presents `RankedUseCasePool` candidates to the AI Transformation Manager and pauses for confirmation."""
    checkpoint = {
        "session_id": session_id,
        "status": "AWAITING_MANAGER_APPROVAL",
        "customer_name": customer_name,
        "industry_vertical": industry_vertical,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    storage_backend = _persist_hitl_checkpoint(session_id, checkpoint)
    return {
        "checkpoint_status": "PAUSED_FOR_HITL_REVIEW",
        "storage_backend": storage_backend,
        "session_id": session_id,
        "message": (
            "Execution paused at HITL Selection Gate. Review the ranked use case candidates "
            "(with relevance scores, customer references, demo links, and capabilities sheet links) "
            "and confirm the selected use cases to proceed to Agent 3 Value & KPI modeling."
        ),
        "candidates_for_review": candidates,
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
) -> dict[str, Any]:
    """Records manager-approved use cases (`ApprovedUseCases`) and removes unchecked items from downstream compute."""
    names_to_use = (
        selected_use_case_names
        if selected_use_case_names is not None
        else (approved_use_case_names or [])
    )
    pool_to_use = candidates if candidates is not None else candidate_pool

    if pool_to_use is None:
        stored = _LOCAL_HITL_SESSION_STORE.get(session_id, {})
        pool_to_use = stored.get("candidates", [])
        customer_name = customer_name or stored.get("customer_name", "")
        industry_vertical = industry_vertical or stored.get("industry_vertical", "")

    selected_lower = {
        str(name).strip().lower() for name in names_to_use if str(name).strip()
    }
    approved = [
        c
        for c in (pool_to_use or [])
        if str(c.get("use_case_name", "")).strip().lower() in selected_lower
    ]

    result = {
        "customer_name": customer_name,
        "industry_vertical": industry_vertical,
        "hitl_status": "APPROVED",
        "hitl_confirmed": True,
        "manager_notes": manager_notes,
        "approved_count": len(approved),
        "approved_use_cases": approved,
    }
    _persist_hitl_checkpoint(
        session_id,
        {
            "session_id": session_id,
            "status": "APPROVED",
            **result,
        },
    )
    return result


hitl_curator_agent = Agent(
    name="solution_curator_hitl",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
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
