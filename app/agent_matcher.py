"""Agent 2: Deterministic Repository Matcher & Ranker (app/agent_matcher.py)."""

from typing import Any

try:
    from google.adk.agents import Agent
    from google.adk.models import Gemini
    from google.genai import types
except ImportError:  # pragma: no cover
    class Agent:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Gemini:  # type: ignore
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class _TypesStub:
        class HttpRetryOptions:
            def __init__(self, **kwargs: Any) -> None:
                self.__dict__.update(kwargs)

    types = _TypesStub()  # type: ignore

from .tools.matcher import (
    DEVELOPMENT_OR_EXCLUDED_KEYWORDS,
    VALID_DEPLOYED_STATUSES,
    RankedUseCasePool,
    UseCaseCandidate,
    is_deployed_and_not_in_development,
    load_repository_from_sqlite,
    match_and_score_use_cases,
    normalize_repository_columns,
)

from .config import build_flash_model, build_gemini_model
from .telemetry import (
    strategic_model_router_callback,
    telemetry_after_model_callback,
    telemetry_after_tool_callback,
    telemetry_before_tool_callback,
    telemetry_on_model_error_callback,
)

matcher_agent = Agent(
    name="repository_matcher_ranker",
    model=build_flash_model(),
    instruction=(
        "You are Agent 2: Deterministic Repository Matcher & Ranker for the Google Cloud "
        "Repeatable, Portable, and Demoable (RPD) Transformation Engine.\n"
        "1. Accept `CustomerContextSchema` (`customer_name`, `industry_vertical`, "
        "`operating_model`, and `friction_points`).\n"
        "2. Invoke `match_and_score_use_cases` with `customer_friction_points` (and optional "
        "`industry_vertical`) to query the verified SQLite use case repository (`fsi_use_cases.db`).\n"
        "3. DETERMINISTIC HARD GATES:\n"
        "   - Hard Gate 1: Only include use cases with a status of 'Deployed - Field Ready' or a verified "
        "deployed status showing it is NOT in any form of development ('Deployed - Referenceable', "
        "'Deployed - Referencable', 'Deployed - 1P GA'). Strictly exclude any use case with a status of "
        "'In Development', 'Conceptual', 'Conceptual Only', 'TBD', 'Deployed - Assets Needed', "
        "'Deployed - Needs Assets', 'Deployed - Not referencable', or blank.\n"
        "   - Hard Gate 2: `has_customer_reference == 1` (named Customer Reference present).\n"
        "   - Any record failing either gate is strictly excluded.\n"
        "4. WEIGHTED SCORING MODEL:\n"
        "   - +40 points for an active Demo Link (`http://` or `https://`).\n"
        "   - +30 points for an active Business Capabilities / Architecture Sheet Link.\n"
        "   - Up to +30 points for semantic friction point matching.\n"
        "5. Return the ranked candidate pool strictly conforming to `RankedUseCasePool`."
    ),
    output_schema=RankedUseCasePool,
    output_key="ranked_use_case_pool",
    before_model_callback=[strategic_model_router_callback],
    after_model_callback=[telemetry_after_model_callback],
    before_tool_callback=[telemetry_before_tool_callback],
    after_tool_callback=[telemetry_after_tool_callback],
    on_model_error_callback=[telemetry_on_model_error_callback],
    tools=[match_and_score_use_cases],
)

__all__ = [
    "VALID_DEPLOYED_STATUSES",
    "DEVELOPMENT_OR_EXCLUDED_KEYWORDS",
    "is_deployed_and_not_in_development",
    "UseCaseCandidate",
    "RankedUseCasePool",
    "load_repository_from_sqlite",
    "normalize_repository_columns",
    "match_and_score_use_cases",
    "matcher_agent",
]
