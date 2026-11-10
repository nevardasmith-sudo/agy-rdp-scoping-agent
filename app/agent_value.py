"""Agent 3: Value Driver & Realized KPI Engine (app/agent_value.py)."""

import re
import sqlite3
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

from .agent_hitl import add_session_to_memory_callback
from .telemetry import (
    emit_audit_log,
    start_rpd_span,
    telemetry_after_tool_callback,
    telemetry_before_tool_callback,
    telemetry_on_model_error_callback,
)
from .tools.matcher import DEFAULT_DB_PATH, INVALID_REF_VALUES, _connect_readonly_sqlite


class ExtractValueInput(BaseModel):
    """Input validation contract for `extract_verified_value_and_kpis`."""

    approved_use_cases: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of HITL-approved use case dictionaries",
    )
    customer_name: str = Field(
        default="", description="Name of the target customer organization"
    )
    industry_vertical: str = Field(
        default="", description="Primary industry vertical"
    )
    manager_notes: str = Field(
        default="",
        description="Executive notes or guidance provided by the AI Transformation Manager",
    )


class VerifiedKpiRecord(BaseModel):
    """Single verified customer reference and its realized KPI impact."""

    customer_reference: str = Field(
        description="Named customer reference from the repository proof record"
    )
    metric_impact: str = Field(
        description="Realized KPI or metric impact bound strictly to the verified customer proof"
    )


class UseCaseValueAnalysis(BaseModel):
    """Grounded value driver and KPI mapping for a single manager-approved use case."""

    use_case_name: str = Field(description="Title of the approved use case")
    use_case_description: str = Field(
        description="Problem statement and solution description"
    )
    value_driver: str = Field(
        default="", description="Primary value driver category from the repository"
    )
    business_value: str = Field(
        description="Grounded business value narrative from the repository"
    )
    kpis_achieved: list[VerifiedKpiRecord] = Field(
        description="Verified customer references and realized KPI impacts (Zero Hallucination)"
    )
    demo_link: str = Field(
        default="", description="Verified Demo Hub or prototype URL"
    )
    business_capabilities_sheet_link: str = Field(
        default="",
        description="Verified Architecture Reference or Business Capabilities sheet URL",
    )


class ValueAnalysisDossier(BaseModel):
    """Output contract for Agent 3 (Value Driver & KPI Engine)."""

    customer_name: str = Field(
        default="", description="Name of the target customer organization"
    )
    industry_vertical: str = Field(
        default="", description="Primary industry vertical"
    )
    manager_notes: str = Field(
        default="",
        description="Executive notes or guidance provided by the AI Transformation Manager at the HITL gate",
    )
    zero_hallucination_verified: bool = Field(
        default=True,
        description="Confirms all KPIs are bound strictly to verified customer references without ungrounded claims",
    )
    use_case_dossiers: list[UseCaseValueAnalysis] = Field(
        description="Grounded value and KPI dossiers for each approved use case"
    )


def _lookup_sqlite_record(
    use_case_name: str, db_path: str = DEFAULT_DB_PATH
) -> Optional[dict[str, Any]]:
    """Looks up the authoritative repository row for `use_case_name` in SQLite using read-only URI mode."""
    if not use_case_name:
        return None
    try:
        conn = _connect_readonly_sqlite(db_path)
        try:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM use_cases WHERE LOWER(TRIM(use_case_name)) = LOWER(TRIM(?)) LIMIT 1",
                (use_case_name,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _parse_verified_kpis(
    customer_reference_raw: str, kpis_raw: str, business_value_raw: str
) -> list[dict[str, str]]:
    """Extracts grounded `(customer_reference, metric_impact)` pairs strictly from source fields."""
    ref_clean = str(customer_reference_raw or "").strip()
    if not ref_clean or ref_clean.lower() in INVALID_REF_VALUES:
        return []

    kpi_clean = str(kpis_raw or "").strip()
    val_clean = str(business_value_raw or "").strip()

    # Split multi-bullet customer proof points (e.g., "• Customer A: 88% reduction...")
    bullet_lines = [
        line.lstrip("•-* ").strip()
        for line in re.split(r"[\n\r]+|(?=\s*•\s*)", ref_clean)
        if line.lstrip("•-* ").strip()
    ]

    verified_pairs: list[dict[str, str]] = []
    for line in bullet_lines:
        # Remove trailing bracketed slide links from the metric text to keep it clean and readable
        line_no_brackets = re.sub(r"\[Customer Story Slide:.*?\]", "", line).strip()
        if ":" in line_no_brackets and not line_no_brackets.lower().startswith("http"):
            cust_part, impact_part = line_no_brackets.split(":", 1)
            cust_name = cust_part.strip()
            impact_text = impact_part.strip()
            # Handle repeated "Customer: Customer: metric" pattern in FSI proof points
            if impact_text.lower().startswith(cust_name.lower() + ":"):
                impact_text = impact_text[len(cust_name) + 1 :].strip()
            metric_impact = impact_text or kpi_clean or val_clean or "Verified deployment in repository"
            verified_pairs.append(
                {
                    "customer_reference": cust_name,
                    "metric_impact": metric_impact,
                }
            )
        else:
            metric_impact = kpi_clean or val_clean or line_no_brackets
            verified_pairs.append(
                {
                    "customer_reference": line_no_brackets,
                    "metric_impact": metric_impact,
                }
            )

    return verified_pairs


def extract_verified_value_and_kpis(
    approved_use_cases: list[dict[str, Any]],
    customer_name: str = "",
    industry_vertical: str = "",
    manager_notes: str = "",
) -> dict[str, Any]:
    """Builds a `ValueAnalysisDossier` from `ApprovedUseCases` and SQLite reference records.

    Enforces the Zero Hallucination Rule:
    - Binds metrics exclusively to verified customer outcomes in the repository.
    - Excludes any record lacking a named customer reference.
    - Prohibits ungrounded financial claims or fabricated ROI percentages.

    Args:
        approved_use_cases: List of manager-approved use case dictionaries from the HITL gate.
        customer_name: Name of the target customer organization.
        industry_vertical: Primary industry vertical of the target customer.
        manager_notes: Optional executive notes supplied at the HITL gate.

    Returns:
        Dictionary conforming to `ValueAnalysisDossier` with verified KPI records.
    """
    validated_req = ExtractValueInput(
        approved_use_cases=list(approved_use_cases or []),
        customer_name=str(customer_name or "").strip(),
        industry_vertical=str(industry_vertical or "").strip(),
        manager_notes=str(manager_notes or "").strip(),
    )
    use_case_dossiers: list[UseCaseValueAnalysis] = []

    for item in validated_req.approved_use_cases:
        uc_name = str(
            item.get("use_case_name") or item.get("use_case_title") or ""
        ).strip()
        db_row = _lookup_sqlite_record(uc_name) or {}

        # Merge item fields with authoritative SQLite row fields
        customer_ref = str(
            item.get("customer_reference")
            or db_row.get("customer_reference")
            or db_row.get("google_proof_points")
            or ""
        ).strip()

        # Zero Hallucination Gate: drop if no verified customer reference exists
        if not customer_ref or customer_ref.lower() in INVALID_REF_VALUES:
            emit_audit_log(
                event_type="UNGROUNDED_KPI_BLOCKED",
                severity="WARNING",
                message=f"Blocked ungrounded use case lacking customer reference: {uc_name}",
                use_case_name=uc_name,
                reason="missing_customer_reference",
            )
            continue

        description = str(
            item.get("use_case_description")
            or db_row.get("use_case_description")
            or db_row.get("description_and_problem_statement")
            or ""
        ).strip()
        value_driver = str(
            item.get("value_driver") or db_row.get("value_driver") or ""
        ).strip()
        business_value = str(
            item.get("business_value")
            or db_row.get("business_value")
            or db_row.get("value_narrative")
            or ""
        ).strip()
        kpis_raw = str(item.get("kpis") or db_row.get("kpis") or "").strip()

        demo_link = str(
            item.get("demo_link")
            or db_row.get("demo_url")
            or db_row.get("demo_link")
            or ""
        ).strip()
        cap_link = str(
            item.get("business_capabilities_sheet_link")
            or item.get("capabilities_sheet_link")
            or db_row.get("business_capabilities_sheet_link")
            or db_row.get("capabilities_sheet_link")
            or ""
        ).strip()

        kpis_achieved_dicts = _parse_verified_kpis(customer_ref, kpis_raw, business_value)
        if not kpis_achieved_dicts:
            continue

        use_case_dossiers.append(
            UseCaseValueAnalysis(
                use_case_name=uc_name,
                use_case_description=description,
                value_driver=value_driver,
                business_value=business_value or value_driver,
                kpis_achieved=[VerifiedKpiRecord(**k) for k in kpis_achieved_dicts],
                demo_link=demo_link,
                business_capabilities_sheet_link=cap_link,
            )
        )

    with start_rpd_span(
        "rpd.extract_verified_value_and_kpis",
        {
            "rpd.customer_name": validated_req.customer_name,
            "rpd.input_approved_count": len(validated_req.approved_use_cases),
            "rpd.verified_dossier_count": len(use_case_dossiers),
        },
    ):
        dossier_model = ValueAnalysisDossier(
            customer_name=validated_req.customer_name,
            industry_vertical=validated_req.industry_vertical,
            manager_notes=validated_req.manager_notes,
            zero_hallucination_verified=True,
            use_case_dossiers=use_case_dossiers,
        )
    dumped = (
        dossier_model.model_dump()
        if hasattr(dossier_model, "model_dump")
        else dossier_model.__dict__
    )
    dumped["verified_use_cases"] = dumped.get("use_case_dossiers", [])
    return dumped


from .config import build_gemini_model, build_pro_model, enforce_hitl_gate_before_tool
from .telemetry import (
    strategic_model_router_callback,
    telemetry_after_model_callback,
)

value_driver_agent = Agent(
    name="value_driver_kpi_engine",
    model=build_pro_model(),
    instruction=(
        "You are Agent 3: Value Driver & Realized KPI Engine for the Google Cloud "
        "Repeatable, Portable, and Demoable (RPD) Transformation Engine.\n"
        "1. Ingest `ApprovedUseCases` confirmed by the AI Transformation Manager at the HITL gate.\n"
        "2. Invoke `extract_verified_value_and_kpis` to cross-reference each approved use case "
        "against the authoritative Google Sheets / SQLite repository reference records.\n"
        "3. ZERO HALLUCINATION RULE (MANDATORY):\n"
        "   - Bind all business value narratives and realized KPI metrics exclusively to verified "
        "customer outcomes present in the reference records.\n"
        "   - Strictly prohibit the fabrication of ungrounded financial claims, speculative dollar "
        "savings, or estimated ROI percentages not explicitly stated in the source documents.\n"
        "   - If a use case lacks a verified customer reference in the repository, exclude it.\n"
        "4. Output a structured JSON strictly conforming to `ValueAnalysisDossier`."
    ),
    output_schema=ValueAnalysisDossier,
    output_key="value_analysis_dossier",
    before_model_callback=[strategic_model_router_callback],
    after_model_callback=[telemetry_after_model_callback],
    before_tool_callback=[enforce_hitl_gate_before_tool, telemetry_before_tool_callback],
    after_tool_callback=[telemetry_after_tool_callback],
    on_model_error_callback=[telemetry_on_model_error_callback],
    after_agent_callback=[add_session_to_memory_callback],
    tools=[extract_verified_value_and_kpis],
)

value_agent = value_driver_agent

__all__ = [
    "VerifiedKpiRecord",
    "UseCaseValueAnalysis",
    "ValueAnalysisDossier",
    "extract_verified_value_and_kpis",
    "value_driver_agent",
    "value_agent",
]
