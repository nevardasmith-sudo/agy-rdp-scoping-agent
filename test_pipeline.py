"""End-to-End Deterministic Pipeline Smoke Test for agy-rpd-scoping-agent."""

import glob
import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)
for sp in glob.glob(os.path.join(ROOT_DIR, ".venv", "lib", "python*", "site-packages")):
    sys.path.insert(0, sp)

from app.agent import (
    app,
    discovery_agent,
    enforce_hitl_gate_before_tool,
    hitl_curator_agent,
    initialize_rpd_session_state,
    matcher_agent,
    opportunity_synthesizer_agent,
    rpd_sequential_pipeline,
    rpd_value_validation_loop,
    value_driver_agent,
)
from app.tools.discovery import _is_safe_public_url, ingest_customer_sources
from app.tools.matcher import _connect_readonly_sqlite, DEFAULT_DB_PATH, match_and_score_use_cases
from app.tools.hitl import present_candidates_for_review, submit_approved_use_cases
from app.tools.value_tools import extract_verified_value_and_kpis
from app.tools.synthesizer import synthesize_transformation_report


class _DummyTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _DummyToolContext:
    def __init__(self, session_id: str) -> None:
        self.invocation_id = session_id
        self.state: dict = {}


def run_e2e_test() -> None:
    # Verify Item 2: Bundled DB path inside app/data/, EventsCompactionConfig, and Session State Initializer
    assert DEFAULT_DB_PATH.endswith(os.path.join("app", "data", "fsi_use_cases.db")), DEFAULT_DB_PATH
    assert app.events_compaction_config is not None
    assert app.events_compaction_config.compaction_interval == 5
    assert app.events_compaction_config.overlap_size == 1

    # Verify Item 3: SequentialAgent, LoopAgent, output_key contracts, and programmatic HITL gate
    assert len(rpd_sequential_pipeline.sub_agents) == 5
    assert len(rpd_value_validation_loop.sub_agents) == 2
    assert discovery_agent.output_key == "customer_context"
    assert matcher_agent.output_key == "ranked_use_case_pool"
    assert hitl_curator_agent.output_key == "approved_use_cases"
    assert value_driver_agent.output_key == "value_analysis_dossier"
    assert opportunity_synthesizer_agent.output_key == "transformation_report"

    dummy_ctx = _DummyToolContext("sess-alpha-001")
    initialize_rpd_session_state(dummy_ctx)  # type: ignore[arg-type]
    assert dummy_ctx.state["session_id"] == "sess-alpha-001"
    assert dummy_ctx.state["hitl_confirmed"] is False

    # Verify before_tool_callback blocks Agent 3 & Agent 4 before HITL approval
    blocked_before_hitl = enforce_hitl_gate_before_tool(
        _DummyTool("extract_verified_value_and_kpis"),  # type: ignore[arg-type]
        {},
        dummy_ctx,  # type: ignore[arg-type]
    )
    assert blocked_before_hitl is not None and blocked_before_hitl["error"] == "HITL_APPROVAL_REQUIRED"

    # 1. Agent 1: Ingest discovery text + verify SSRF protection against metadata & loopback IPs
    assert not _is_safe_public_url("http://169.254.169.254/computeMetadata/v1/")
    assert not _is_safe_public_url("http://127.0.0.1:8000/admin")
    assert not _is_safe_public_url("http://metadata.google.internal/computeMetadata/v1/")
    ingested = ingest_customer_sources(
        urls=["http://169.254.169.254/computeMetadata/v1/"],
        discovery_passages=[
            "Global insurer experiencing manual claims triage delays, fragmented underwriting documents, and high call center handle times. Contact Jane Doe at jane.doe@acmeinsurance.com or SSN 123-45-6789 or 415-555-0199."
        ],
    )
    assert len(ingested["blocked_ssrf_urls"]) == 1
    assert ingested["pii_redactions_applied"] >= 3
    assert "jane.doe@acmeinsurance.com" not in ingested["combined_context"]
    assert "123-45-6789" not in ingested["combined_context"]
    assert "[REDACTED_EMAIL]" in ingested["combined_context"]
    assert "[REDACTED_SSN]" in ingested["combined_context"]
    assert "[REDACTED_PHONE]" in ingested["combined_context"]
    print(
        "1. Discovery Ingested:",
        ingested["passage_count"],
        "passage(s) | Blocked SSRF URLs:",
        len(ingested["blocked_ssrf_urls"]),
        "| PII Redactions:",
        ingested["pii_redactions_applied"],
    )

    # Verify read-only SQLite URI enforcement blocks write attempts
    ro_conn = _connect_readonly_sqlite(DEFAULT_DB_PATH)
    try:
        try:
            ro_conn.execute("DELETE FROM use_cases")
            raise AssertionError("Expected sqlite3.OperationalError on write to read-only DB")
        except Exception as exc:
            assert "readonly" in str(exc).lower()
    finally:
        ro_conn.close()

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

    # 3. HITL Gateway: Present & Approve top 2 use cases with isolated ToolContext state
    enforce_hitl_gate_before_tool(
        _DummyTool("present_candidates_for_review"),  # type: ignore[arg-type]
        {},
        dummy_ctx,  # type: ignore[arg-type]
    )
    checkpoint = present_candidates_for_review(
        candidates[:5],
        customer_name="Acme Insurance Group",
        industry_vertical="Financial Services & Insurance (FSI) - Insurance",
        tool_context=dummy_ctx,  # type: ignore[arg-type]
    )
    assert checkpoint["session_id"] == "sess-alpha-001"
    assert dummy_ctx.state["hitl_status"] == "PAUSED_FOR_HITL_REVIEW"

    # Attempt to auto-approve in the exact same turn must be blocked by before_tool_callback
    same_turn_violation = enforce_hitl_gate_before_tool(
        _DummyTool("submit_approved_use_cases"),  # type: ignore[arg-type]
        {},
        dummy_ctx,  # type: ignore[arg-type]
    )
    assert same_turn_violation is not None and same_turn_violation["error"] == "HITL_GATE_VIOLATION"

    # Simulate next user turn where the AI Transformation Manager replies with approval
    dummy_ctx.invocation_id = "sess-alpha-002-turn2"
    next_turn_allowed = enforce_hitl_gate_before_tool(
        _DummyTool("submit_approved_use_cases"),  # type: ignore[arg-type]
        {},
        dummy_ctx,  # type: ignore[arg-type]
    )
    assert next_turn_allowed is None

    approval = submit_approved_use_cases(
        approved_use_case_names=[c["use_case_name"] for c in candidates[:2]],
        manager_notes="Prioritize claims and underwriting quick wins.",
        tool_context=dummy_ctx,  # type: ignore[arg-type]
    )
    assert approval["session_id"] == "sess-alpha-001"
    assert dummy_ctx.state["hitl_confirmed"] is True
    print(
        "3. HITL Status:",
        approval["hitl_status"],
        "| Session:",
        approval["session_id"],
        "| Approved:",
        len(approval["approved_use_cases"]),
    )

    # 4. Agent 3: Extract Zero-Hallucination KPIs (including ungrounded item rejection test)
    dossier = extract_verified_value_and_kpis(
        approved_use_cases=approval["approved_use_cases"]
        + [{"use_case_name": "Ungrounded Speculative AI Idea", "customer_reference": "TBD"}],
        customer_name="Acme Insurance Group",
        industry_vertical="Financial Services & Insurance (FSI) - Insurance",
    )
    assert len(dossier["verified_use_cases"]) == 2
    print("4. Value Dossier Verified Records:", len(dossier["verified_use_cases"]))

    # 5. Agent 4: Synthesize Executive Opportunity Report
    report = synthesize_transformation_report(dossier)

    # Verify Item 4: OpenTelemetry spans, Structured Audit Logs, Callbacks, and FastAPI /healthz
    from app.config import FLASH_MODEL_NAME, PRO_MODEL_NAME
    from app.fast_api_app import healthz
    from app.telemetry import (
        AUDIT_LOG_BUFFER,
        SPAN_EXPORTER,
        emit_audit_log,
        scrub_pii,
        scrub_session_and_memory_state,
        strategic_model_router_callback,
        telemetry_after_model_callback,
        telemetry_after_tool_callback,
        telemetry_before_tool_callback,
        telemetry_on_model_error_callback,
    )

    # Verify PII scrubbing in logging and session memory pipelines
    scrubbed_log = emit_audit_log(
        event_type="CUSTOMER_NOTE_LOGGED",
        message="Manager email is bob@acme.com and card is 4111-2222-3333-4444",
        api_key="secret_key_value",
    )
    assert "[REDACTED_EMAIL]" in scrubbed_log["message"]
    assert "[REDACTED_CREDIT_CARD]" in scrubbed_log["message"]
    assert scrubbed_log["api_key"] == "[REDACTED_SENSITIVE_FIELD]"
    assert scrubbed_log["pii_scrubbed"] is True

    dummy_ctx.state["manager_contact"] = "Reach out to alice@acme.com or 123-45-6789"
    redactions_in_memory = scrub_session_and_memory_state(dummy_ctx)
    assert redactions_in_memory >= 2
    assert "[REDACTED_EMAIL]" in dummy_ctx.state["manager_contact"]
    assert "[REDACTED_SSN]" in dummy_ctx.state["manager_contact"]

    # Verify Strategic Model Routing across sub-agents (Flash vs Pro delegation)
    assert discovery_agent.model.model == FLASH_MODEL_NAME
    assert matcher_agent.model.model == FLASH_MODEL_NAME
    assert hitl_curator_agent.model.model == FLASH_MODEL_NAME
    assert value_driver_agent.model.model == PRO_MODEL_NAME
    assert opportunity_synthesizer_agent.model.model == PRO_MODEL_NAME

    # Verify dynamic before_model_callback routes pre-HITL to Flash and post-HITL / complex synthesis to Pro
    class _DummyLlmRequest:
        def __init__(self, contents: str) -> None:
            self.model = ""
            self.contents = contents

    class _DummyUsage:
        prompt_token_count = 120
        candidates_token_count = 80
        total_token_count = 200

    class _DummyLlmResponse:
        usage_metadata = _DummyUsage()

    dummy_ctx.state["hitl_confirmed"] = False
    req_flash = _DummyLlmRequest("Match Insurance use cases")
    strategic_model_router_callback(dummy_ctx, req_flash)  # type: ignore[arg-type]
    assert req_flash.model == FLASH_MODEL_NAME
    assert dummy_ctx.state["active_model_tier"] == "FLASH_LOW_LATENCY"

    dummy_ctx.state["hitl_confirmed"] = True
    req_pro = _DummyLlmRequest("Synthesize executive transformation report")
    strategic_model_router_callback(dummy_ctx, req_pro)  # type: ignore[arg-type]
    assert req_pro.model == PRO_MODEL_NAME
    assert dummy_ctx.state["active_model_tier"] == "PRO_COMPLEX_REASONING"
    telemetry_after_model_callback(dummy_ctx, _DummyLlmResponse())  # type: ignore[arg-type]

    telemetry_before_tool_callback(_DummyTool("match_and_score_use_cases"), {"industry_vertical": "Insurance"}, dummy_ctx)  # type: ignore[arg-type]
    telemetry_after_tool_callback(_DummyTool("match_and_score_use_cases"), {"industry_vertical": "Insurance"}, dummy_ctx, {"status": "ok"})  # type: ignore[arg-type]
    telemetry_on_model_error_callback(dummy_ctx, None, RuntimeError("simulated model timeout"))  # type: ignore[arg-type]

    event_types = {entry["rpd_event_type"] for entry in AUDIT_LOG_BUFFER}
    for expected_event in (
        "SSRF_URL_BLOCKED",
        "HARD_GATE_FILTER",
        "HITL_DECISION",
        "UNGROUNDED_KPI_BLOCKED",
        "REPORT_SYNTHESIZED",
        "TOOL_INVOCATION_START",
        "TOOL_INVOCATION_COMPLETE",
        "LLM_MODEL_ERROR",
        "MODEL_ROUTING_DECISION",
        "LLM_TOKEN_USAGE",
        "PII_REDACTED",
    ):
        assert expected_event in event_types, f"Missing audit event: {expected_event}"

    if SPAN_EXPORTER is not None:
        span_names = {s.name for s in SPAN_EXPORTER.get_finished_spans()}
        assert "rpd.match_and_score_use_cases" in span_names
        assert "rpd.extract_verified_value_and_kpis" in span_names
        assert "rpd.synthesize_transformation_report" in span_names
        assert "rpd.strategic_model_router" in span_names

    health_status = healthz()
    assert health_status["status"] == "ok" and health_status["repository_db_ready"] is True

    # Verify Item 5: Infrastructure, CI/CD, Dockerfile, eval_config.json, and 4-case rpd_eval_set.json
    import json

    assert os.path.exists(os.path.join(ROOT_DIR, "Dockerfile"))
    assert os.path.exists(os.path.join(ROOT_DIR, ".antigravity", "github-actions-ci-cd.yml"))
    assert os.path.exists(os.path.join(ROOT_DIR, ".antigravity", "pipeline.yaml"))

    with open(os.path.join(ROOT_DIR, "eval_config.json"), encoding="utf-8") as f:
        eval_cfg = json.load(f)
    assert "tool_trajectory_avg_score" in eval_cfg["criteria"]

    with open(os.path.join(ROOT_DIR, "rpd_eval_set.json"), encoding="utf-8") as f:
        eval_set = json.load(f)
    assert len(eval_set["eval_cases"]) == 4

    print("5. Final Report Markdown Preview:\n")
    print(report["formatted_markdown"][:1200])


if __name__ == "__main__":
    run_e2e_test()




