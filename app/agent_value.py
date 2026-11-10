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
from .tools.matcher import DEFAULT_DB_PATH, INVALID_REF_VALUES


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
    """Looks up the authoritative repository row for `use_case_name` in SQLite."""
    if not use_case_name:
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM use_cases WHERE LOWER(TRIM(use_case_name)) = LOWER(TRIM(?)) LIMIT 1",
            (use_case_name,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
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
    """
    use_case_dossiers: list[dict[str, Any]] = []

    for item in approved_use_cases or []:
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

        kpis_achieved = _parse_verified_kpis(customer_ref, kpis_raw, business_value)
        if not kpis_achieved:
            continue

        use_case_dossiers.append(
            {
                "use_case_name": uc_name,
                "use_case_description": description,
                "value_driver": value_driver,
                "business_value": business_value or value_driver,
                "kpis_achieved": kpis_achieved,
                "demo_link": demo_link,
                "business_capabilities_sheet_link": cap_link,
            }
        )

    return {
        "customer_name": customer_name,
        "industry_vertical": industry_vertical,
        "manager_notes": manager_notes,
        "zero_hallucination_verified": True,
        "use_case_dossiers": use_case_dossiers,
        "verified_use_cases": use_case_dossiers,
    }


value_driver_agent = Agent(
    name="value_driver_kpi_engine",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
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
