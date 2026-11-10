"""Shared Model Configuration, HITL Governance Callbacks, and Workflow Checkers (app/config.py)."""

import os
from typing import Any, AsyncGenerator, Optional

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)
except ImportError:  # pragma: no cover
    pass

try:
    from google.adk.agents import BaseAgent
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event, EventActions
    from google.adk.models import Gemini
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext
    from google.genai import types
except ImportError:  # pragma: no cover
    class BaseAgent:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class InvocationContext:  # type: ignore
        pass

    class Event:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class EventActions:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Gemini:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class BaseTool:  # type: ignore
        name: str = ""

    class ToolContext:  # type: ignore
        pass

    class _TypesStub:
        class HttpRetryOptions:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)

    types = _TypesStub()  # type: ignore

# Bypass token sharing blockade for downstream GCP tool calls
os.environ["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "False"

PROJECT_ID = os.environ.get(
    "RPD_GOOGLE_CLOUD_PROJECT",
    os.environ.get("GOOGLE_CLOUD_PROJECT", "nevardaai5days-sandbox-232845"),
)
API_KEY = os.environ.get("GOOGLE_API_KEY")

if not API_KEY:
    os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
    os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
else:
    os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    os.environ.pop("GOOGLE_CLOUD_LOCATION", None)
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")

_DEFAULT_MODEL = (
    "gemini-flash-latest"
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() in ("false", "0")
    else "gemini-2.5-flash"
)
MODEL_NAME = os.environ.get("RPD_GEMINI_MODEL", _DEFAULT_MODEL)


def build_gemini_model(model_name: Optional[str] = None) -> Gemini:
    """Constructs a configured `Gemini` instance with retry options and API key / Vertex AI settings."""
    use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() not in (
        "false",
        "0",
    )
    client_kwargs = None
    if API_KEY and not use_vertex:
        client_kwargs = {"api_key": API_KEY, "vertexai": False}
    elif API_KEY:
        client_kwargs = {"api_key": API_KEY}

    return Gemini(
        model=model_name or MODEL_NAME,
        retry_options=types.HttpRetryOptions(attempts=3),
        client_kwargs=client_kwargs,
    )


def enforce_hitl_gate_before_tool(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> Optional[dict[str, Any]]:
    """Deterministic `before_tool_callback` enforcing the Human-in-the-Loop (HITL) selection gate.

    Rules:
    1. When `present_candidates_for_review` runs, records the current `invocation_id` in
       `tool_context.state["hitl_presented_turn"]` and sets `hitl_confirmed = False`.
    2. When `submit_approved_use_cases` runs, blocks the LLM if it attempts to self-approve
       candidates within the exact same turn (`invocation_id`) that it presented them.
    3. When `extract_verified_value_and_kpis` or `synthesize_transformation_report` runs,
       blocks execution if `hitl_confirmed` is not True.
    """
    tool_name = getattr(tool, "name", "") or str(tool)
    state = getattr(tool_context, "state", None)
    if state is None:
        return None

    current_turn = str(getattr(tool_context, "invocation_id", "") or "")

    if tool_name == "present_candidates_for_review":
        state["hitl_presented_turn"] = current_turn or "turn_presented"
        state["hitl_status"] = "PAUSED_FOR_HITL_REVIEW"
        state["hitl_confirmed"] = False
        return None

    if tool_name == "submit_approved_use_cases":
        presented_turn = str(state.get("hitl_presented_turn") or "")
        if presented_turn and current_turn and presented_turn == current_turn:
            return {
                "error": "HITL_GATE_VIOLATION",
                "hitl_status": "PAUSED_FOR_HITL_REVIEW",
                "message": (
                    "MANDATORY GOVERNANCE GATE: You cannot call `submit_approved_use_cases` in the "
                    "same turn as `present_candidates_for_review`. Stop and wait for the human AI "
                    "Transformation Manager to reply with their approved use cases in the next turn."
                ),
            }
        state["hitl_confirmed"] = True
        state["hitl_status"] = "APPROVED"
        return None

    if tool_name in ("extract_verified_value_and_kpis", "synthesize_transformation_report"):
        if not state.get("hitl_confirmed", False):
            return {
                "error": "HITL_APPROVAL_REQUIRED",
                "hitl_status": state.get("hitl_status", "AWAITING_MANAGER_APPROVAL"),
                "message": (
                    f"Cannot execute `{tool_name}` before human confirmation at the HITL gate. "
                    "Present candidates via `present_candidates_for_review`, wait for the user's "
                    "selection in the next turn, and record approval via `submit_approved_use_cases`."
                ),
            }

    return None


class EscalationChecker(BaseAgent):
    """Deterministic `BaseAgent` loop controller that escalates once HITL & Zero-Hallucination gates pass."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = getattr(ctx.session, "state", {}) if getattr(ctx, "session", None) else {}
        dossier = state.get("value_analysis_dossier")
        is_verified = bool(
            (isinstance(dossier, dict) and dossier.get("zero_hallucination_verified"))
            or state.get("zero_hallucination_verified")
        )
        if is_verified:
            yield Event(author=self.name, actions=EventActions(escalate=True))
        else:
            yield Event(author=self.name)
