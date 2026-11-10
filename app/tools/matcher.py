"""Deterministic Repository Matcher & Ranker tool (app/tools/matcher.py)."""

import os
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
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore

from ..telemetry import emit_audit_log, start_rpd_span

_APP_DATA_DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "fsi_use_cases.db")
)
_BUNDLED_DB_PATH = (
    _APP_DATA_DB_PATH
    if os.path.exists(_APP_DATA_DB_PATH)
    else os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "fsi_use_cases.db")
    )
)
DEFAULT_DB_PATH = os.environ.get(
    "RPD_SQLITE_DB_PATH",
    _BUNDLED_DB_PATH
    if os.path.exists(_BUNDLED_DB_PATH)
    else "/usr/local/google/home/nevardasmith/fsi_use_cases.db",
)

INVALID_REF_VALUES = {"", "none", "tbd", "n/a", "idk if we have anything for this"}

VALID_DEPLOYED_STATUSES = {
    "DEPLOYED - FIELD READY",
    "DEPLOYED- FIELD READY",
    "DEPLOYED - REFERENCEABLE",
    "DEPLOYED - REFERENCABLE",
    "DEPLOYED - 1P GA",
}

DEVELOPMENT_OR_EXCLUDED_KEYWORDS = (
    "IN DEVELOPMENT",
    "UNDER DEVELOPMENT",
    "DEVELOPMENT",
    "IN PROGRESS",
    "CONCEPTUAL",
    "IDEATION",
    "TBD",
    "PLANNED",
    "ROADMAP",
    "DRAFT",
    "ASSETS NEEDED",
    "NEEDS ASSETS",
    "NOT REFERENCABLE",
    "NOT REFERENCEABLE",
    "PENDING",
    "PILOT",
    "POC",
)


def is_deployed_and_not_in_development(status_val: Any) -> bool:
    """Returns True only if status is 'Deployed - Field Ready' or a verified deployed status not in any form of development."""
    status_upper = re.sub(r"\s+", " ", str(status_val or "").strip().upper())
    if not status_upper:
        return False
    if any(kw in status_upper for kw in DEVELOPMENT_OR_EXCLUDED_KEYWORDS):
        return False
    return status_upper in VALID_DEPLOYED_STATUSES


class MatchUseCasesInput(BaseModel):
    """Input validation contract for `match_and_score_use_cases`."""

    customer_friction_points: list[str] = Field(
        default_factory=list,
        description="List of customer business and operational friction points to match against the repository",
    )
    industry_vertical: Optional[str] = Field(
        default=None,
        description="Optional target industry vertical or sub-industry filter (e.g., Insurance, Banking, Retail)",
    )
    top_k: int = Field(
        default=25,
        ge=1,
        le=150,
        description="Maximum number of ranked use case candidates to return (1 to 150)",
    )


class UseCaseCandidate(BaseModel):
    """Single verified and scored use case candidate."""

    use_case_name: str = Field(description="Title of the verified use case")
    use_case_description: str = Field(
        description="Problem statement and solution description"
    )
    industry_domain: str = Field(
        default="", description="Primary industry domain in the repository"
    )
    sub_industry: str = Field(
        default="", description="Sub-industry or pillar classification"
    )
    status: str = Field(
        default="Deployed - Field Ready", description="Normalized RPD deployment status"
    )
    customer_reference: str = Field(
        description="Verified customer proof point(s) from the repository"
    )
    demo_link: str = Field(
        default="", description="Active Demo Hub or interactive prototype URL"
    )
    capabilities_sheet_link: str = Field(
        default="",
        description="Architecture reference deck or Business Capabilities sheet URL",
    )
    business_value: str = Field(
        default="", description="Grounded business value narrative"
    )
    kpis: str = Field(default="", description="Grounded KPIs from the repository")
    score: int = Field(
        description="Deterministic relevance score (0-100: +40 Demo, +30 Capabilities Link, +30 Semantic Fit)"
    )


class RankedUseCasePool(BaseModel):
    """Output contract for Agent 2 (Repository Matcher & Ranker)."""

    total_matching_count: int = Field(
        description="Total number of use cases passing Hard Gate 1 and Hard Gate 2"
    )
    industry_filter_applied: str = Field(
        default="All",
        description="Industry domain or sub-industry filter applied during query",
    )
    candidates: list[UseCaseCandidate] = Field(
        description="Ranked list of verified use case candidates ordered by score descending"
    )


def _extract_first_http_url(val: Any) -> str:
    """Extracts the first http(s):// URL from a cell string if present."""
    if not val:
        return ""
    s = str(val).strip()
    if s.startswith(("http://", "https://")):
        return s.split()[0].strip()
    m = re.search(r"https?://[^\s\)\]]+", s)
    return m.group(0).strip() if m else ""


def normalize_repository_columns(df: Any) -> Any:
    """Normalizes column header aliases across tracking sheets."""
    column_mapping = {
        "Status": "status",
        "Deployment Status": "status",
        "USE CASE STATUS": "status",
        "Field Ready": "status",
        "Use Case Title": "use_case_name",
        "Description and Problem Statement": "use_case_description",
        "Customer Ref": "customer_reference",
        "Customer Reference": "customer_reference",
        "Google Proof Points (go/CustomerReferenceLibrary_FSI)": "customer_reference",
        "Google Proof Points (go/CustomerReferenceLibrary_HCLS)": "customer_reference",
        "Demo Hub Link": "demo_link",
        "Demo Link": "demo_link",
        "Demo Links": "demo_link",
        "Architecture Link": "capabilities_sheet_link",
        "Architecture Reference [Link]": "capabilities_sheet_link",
        "Business Capabilities Sheet Link": "capabilities_sheet_link",
    }
    if pd is not None and isinstance(df, pd.DataFrame):
        return df.rename(columns=column_mapping)
    if isinstance(df, list):
        normalized: list[dict[str, Any]] = []
        for row in df:
            new_row = dict(row)
            for src, dst in column_mapping.items():
                if src in new_row and dst not in new_row:
                    new_row[dst] = new_row[src]
            normalized.append(new_row)
        return normalized
    return df


def _connect_readonly_sqlite(db_path: str) -> sqlite3.Connection:
    """Opens a SQLite database connection in strict read-only URI mode (`?mode=ro`)."""
    abs_path = os.path.abspath(db_path)
    try:
        conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
    except sqlite3.Error:
        conn = sqlite3.connect(abs_path)
    conn.row_factory = sqlite3.Row
    return conn


def _extract_stem_variants(word: str) -> set[str]:
    """Generates normalized token and lightweight stem variants for semantic friction matching."""
    clean = re.sub(r"[^a-z0-9]", "", word.lower())
    if len(clean) <= 3:
        return set()
    variants = {clean}
    for suffix in ("ments", "ment", "tion", "sion", "ings", "ing", "ities", "ity", "ed", "es", "s"):
        if clean.endswith(suffix) and len(clean) - len(suffix) >= 4:
            variants.add(clean[: -len(suffix)])
    return variants


def load_repository_from_sqlite(
    industry_vertical: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Loads deployed, non-development use case records from the unified `use_cases` table in SQLite.

    Args:
        industry_vertical: Optional industry domain or sub-industry filter.
        db_path: Filesystem path to the SQLite database (`fsi_use_cases.db`).

    Returns:
        List of dictionary records representing verified deployed use cases.
    """
    if not os.path.exists(db_path):
        return []

    conn = _connect_readonly_sqlite(db_path)
    try:
        cur = conn.cursor()
        rows = [
            dict(r)
            for r in cur.execute("SELECT * FROM use_cases").fetchall()
            if is_deployed_and_not_in_development(
                r["raw_status"] if "raw_status" in r.keys() and r["raw_status"] else r["status"]
            )
        ]
    finally:
        conn.close()

    if not industry_vertical or not str(industry_vertical).strip():
        return rows

    iv_lower = str(industry_vertical).strip().lower()
    filtered = [
        r
        for r in rows
        if iv_lower in str(r.get("industry_domain", "")).lower()
        or iv_lower in str(r.get("sub_industry", "")).lower()
        or str(r.get("industry_domain", "")).lower() in iv_lower
        or str(r.get("sub_industry", "")).lower() in iv_lower
    ]
    # If industry vertical matches a subset, return that subset; otherwise return all rows
    return filtered if filtered else rows


def _passes_hard_gate_1(row: dict[str, Any]) -> bool:
    """Hard Gate 1: Deployment Maturity — only allow 'Deployed - Field Ready' or verified deployed statuses not in any form of development."""
    raw_status = str(
        row.get("raw_status") or row.get("use_case_status") or ""
    ).strip()
    norm_status = str(row.get("status") or "").strip()

    if raw_status:
        return is_deployed_and_not_in_development(raw_status)
    if norm_status:
        return is_deployed_and_not_in_development(norm_status)

    if "is_field_ready" in row or "is_deployed_referenceable" in row:
        try:
            is_fr = int(row.get("is_field_ready") or 0) == 1
        except (ValueError, TypeError):
            is_fr = str(row.get("is_field_ready", "")).strip().upper() == "TRUE"
        try:
            is_dr = int(row.get("is_deployed_referenceable") or 0) == 1
        except (ValueError, TypeError):
            is_dr = str(row.get("is_deployed_referenceable", "")).strip().upper() == "TRUE"
        return is_fr or is_dr

    return False


def _passes_hard_gate_2(row: dict[str, Any]) -> bool:
    """Hard Gate 2: Customer Validation (`has_customer_reference` and non-empty `customer_reference`)."""
    ref_str = str(row.get("customer_reference") or "").strip()
    if not ref_str or ref_str.lower() in INVALID_REF_VALUES:
        return False

    if "has_customer_reference" in row and row.get("has_customer_reference") is not None:
        try:
            return int(row.get("has_customer_reference") or 0) == 1
        except (ValueError, TypeError):
            return str(row.get("has_customer_reference", "")).strip().upper() == "TRUE"

    return True


def match_and_score_use_cases(
    customer_friction_points: list[str],
    repository_data: Optional[list[dict[str, Any]]] = None,
    industry_vertical: Optional[str] = None,
    top_k: int = 25,
) -> list[dict[str, Any]]:
    """Filters and ranks use cases from the repository based on hard gates and weighted criteria.

    Enforces two mandatory deterministic governance gates:
    - Hard Gate 1: `is_deployed_and_not_in_development` (`status == 'Deployed - Field Ready'`)
    - Hard Gate 2: `has_customer_reference` (named `customer_reference` present)

    Weighted Scoring Model (0 to 100 points):
    - +40 points for active Demo Link (`http://` or `https://`)
    - +30 points for Business Capabilities / Architecture Sheet Link (`http://` or `https://`)
    - Up to +30 points for stem-aware semantic friction point fit

    Args:
        customer_friction_points: List of customer business and operational friction points.
        repository_data: Optional pre-loaded list of repository dictionaries (defaults to SQLite query).
        industry_vertical: Optional target industry vertical or sub-industry filter.
        top_k: Maximum number of top-ranked candidates to return (default 25, bounded 1..150).

    Returns:
        Ranked list of verified use case candidate dictionaries ordered by score descending.
    """
    validated_input = MatchUseCasesInput(
        customer_friction_points=[str(fp) for fp in (customer_friction_points or []) if str(fp).strip()],
        industry_vertical=str(industry_vertical).strip() if industry_vertical else None,
        top_k=max(1, min(int(top_k or 25), 150)),
    )
    clean_frictions = validated_input.customer_friction_points
    clean_vertical = validated_input.industry_vertical
    limit_k = validated_input.top_k

    if repository_data is None:
        repository_data = load_repository_from_sqlite(industry_vertical=clean_vertical)

    if not repository_data:
        return []

    if pd is not None:
        df = normalize_repository_columns(pd.DataFrame(repository_data))
        if df.empty:
            return []
        records: list[dict[str, Any]] = df.to_dict(orient="records")
    else:
        records = normalize_repository_columns(repository_data)

    # Enforce Hard Gate 1 & Hard Gate 2 with OpenTelemetry span & structured audit logging
    with start_rpd_span(
        "rpd.match_and_score_use_cases",
        {
            "rpd.industry_vertical": str(clean_vertical or "all"),
            "rpd.friction_point_count": len(clean_frictions),
            "rpd.total_repo_candidates": len(records),
        },
    ) as span:
        qualified_records = [
            r for r in records if _passes_hard_gate_1(r) and _passes_hard_gate_2(r)
        ]
        rejected_count = len(records) - len(qualified_records)
        span.set_attribute("rpd.qualified_candidates", len(qualified_records))
        span.set_attribute("rpd.rejected_by_hard_gates", rejected_count)
        emit_audit_log(
            event_type="HARD_GATE_FILTER",
            severity="INFO",
            message=(
                f"Hard Gates 1 & 2 evaluated {len(records)} records: "
                f"{len(qualified_records)} qualified, {rejected_count} rejected."
            ),
            industry_vertical=str(clean_vertical or "all"),
            total_repo_candidates=len(records),
            qualified_candidates=len(qualified_records),
            rejected_by_hard_gates=rejected_count,
        )

    ranked_results: list[dict[str, Any]] = []
    for row in qualified_records:
        score = 0

        # Priority Boost 1: Active Demo Link (+40)
        raw_demo = str(row.get("demo_url") or row.get("demo_link") or "").strip()
        demo_url = _extract_first_http_url(raw_demo)
        if demo_url and demo_url.lower() != "none" and demo_url.startswith(("http://", "https://")):
            score += 40

        # Priority Boost 2: Architectural Depth / Business Capabilities Sheet Link (+30)
        raw_arch = str(
            row.get("business_capabilities_sheet_link")
            or row.get("capabilities_sheet_link")
            or ""
        ).strip()
        arch_url = _extract_first_http_url(raw_arch)
        if arch_url and arch_url.lower() != "none" and arch_url.startswith(("http://", "https://")):
            score += 30

        # Priority Boost 3: Stem-Aware Semantic Friction Fit (+30)
        searchable_blob = " ".join(
            [
                str(row.get("use_case_name") or ""),
                str(row.get("use_case_description") or ""),
                str(row.get("business_capabilities") or ""),
                str(row.get("business_value") or ""),
                str(row.get("value_driver") or ""),
            ]
        ).lower()

        matched_terms = 0
        for friction in clean_frictions:
            stem_candidates: set[str] = set()
            for w in str(friction).split():
                stem_candidates.update(_extract_stem_variants(w))
            if any(stem in searchable_blob for stem in stem_candidates):
                matched_terms += 1

        if clean_frictions:
            semantic_score = min(
                30, int((matched_terms / len(clean_frictions)) * 30)
            )
            score += semantic_score

        candidate_model = UseCaseCandidate(
            use_case_name=str(row.get("use_case_name") or ""),
            use_case_description=str(row.get("use_case_description") or ""),
            industry_domain=str(row.get("industry_domain") or ""),
            sub_industry=str(row.get("sub_industry") or ""),
            status="Deployed - Field Ready",
            customer_reference=str(row.get("customer_reference") or ""),
            demo_link=demo_url or raw_demo,
            capabilities_sheet_link=arch_url or raw_arch,
            business_value=str(row.get("business_value") or ""),
            kpis=str(row.get("kpis") or ""),
            score=score,
        )
        ranked_results.append(
            candidate_model.model_dump()
            if hasattr(candidate_model, "model_dump")
            else candidate_model.__dict__
        )

    ranked_results.sort(key=lambda x: x["score"], reverse=True)
    return ranked_results[:limit_k]


if __name__ == "__main__":
    sample_frictions = [
        "manual claims triage and fragmented underwriting documents",
        "slow customer onboarding and legacy call center wait times",
        "supply chain disruptions and demand forecasting inaccuracy",
    ]
    matches = match_and_score_use_cases(customer_friction_points=sample_frictions)
    print(f"Total verified use cases passing Hard Gates 1 & 2: {len(matches)}")
    for idx, item in enumerate(matches[:10], 1):
        print(
            f"  {idx:>2}. [{item['score']:>3} pts] ({item['sub_industry']}) "
            f"{item['use_case_name']} | Demo: {bool(item['demo_link'])} | Arch: {bool(item['capabilities_sheet_link'])}"
        )
