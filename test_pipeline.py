"""End-to-End Deterministic Pipeline Smoke Test for agy-rpd-scoping-agent."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.agent_discovery import ingest_customer_sources
from app.tools.matcher import match_and_score_use_cases
from app.agent_hitl import present_candidates_for_review, submit_approved_use_cases
from app.agent_value import extract_verified_value_and_kpis
from app.agent_synthesizer import synthesize_transformation_report


def run_e2e_test() -> None:
    # 1. Agent 1: Ingest discovery text (and test HTTP fallback)
    ingested = ingest_customer_sources(
        urls=[],
        discovery_passages=[
            "Global insurer experiencing manual claims triage delays, fragmented underwriting documents, and high call center handle times."
        ],
    )
    print("1. Discovery Ingested:", ingested["passage_count"], "passage(s)")

    # 2. Agent 2: Match & Score against fsi_use_cases.db
    candidates = match_and_score_use_cases(
        customer_friction_points=[
            "manual claims triage",
            "fragmented underwriting documents",
            "call center handle times",
        ],
        industry_vertical="Insurance",
    )
    print(
        f"2. Matched {len(candidates)} verified Insurance use cases. Top match: "
        f"{candidates[0]['use_case_name']} ({candidates[0]['score']} pts)"
    )

    # 3. HITL Gateway: Present & Approve top 2 use cases
    checkpoint = present_candidates_for_review(candidates[:5])
    approval = submit_approved_use_cases(
        approved_use_case_names=[c["use_case_name"] for c in candidates[:2]],
        candidate_pool=candidates,
        manager_notes="Prioritize claims and underwriting quick wins.",
    )
    print(
        "3. HITL Status:",
        approval["hitl_status"],
        "| Approved:",
        len(approval["approved_use_cases"]),
    )

    # 4. Agent 3: Extract Zero-Hallucination KPIs
    dossier = extract_verified_value_and_kpis(
        approved_use_cases=approval["approved_use_cases"],
        customer_name="Acme Insurance Group",
        industry_vertical="Financial Services & Insurance (FSI) - Insurance",
    )
    print("4. Value Dossier Verified Records:", len(dossier["verified_use_cases"]))

    # 5. Agent 4: Synthesize Executive Opportunity Report
    report = synthesize_transformation_report(dossier)
    print("5. Final Report Markdown Preview:\n")
    print(report["formatted_markdown"][:1200])


if __name__ == "__main__":
    run_e2e_test()
