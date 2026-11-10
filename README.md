# agy-rpd-scoping-agent (`v1.0.0` — Production Hardened Release)

Repeatable, Portable, and Demoable (RPD) Transformation Engine built on the Google Agent Development Kit (ADK) for the Gemini Enterprise Agent Platform.

## Production Hardening Highlights (`v1.0.0` — 100/100 Scorecard)

1. **Tool & Interface Design (`app/tools/`)**:
   - SSRF & Private IP protection (`_is_safe_public_url`) blocking loopback, link-local (`169.254.169.254`), and RFC1918 addresses in `ingest_customer_sources`.
   - Read-only SQLite URI enforcement (`?mode=ro`) across `load_repository_from_sqlite` and `_lookup_sqlite_record`.
   - Strict Pydantic input/output validation (`IngestSourcesInput`, `MatchUseCasesInput`, `ApprovedUseCases`, `ExtractValueInput`, `ValueAnalysisDossier`, `TransformationOpportunityReport`) and stem-aware semantic friction matching.
2. **Context & Memory (`app/agent.py`, `app/agent_hitl.py`)**:
   - Deterministic `initialize_rpd_session_state` (`before_agent_callback`) and per-session `ToolContext` isolation.
   - Sliding-window `EventsCompactionConfig` (`compaction_interval=5, overlap_size=1`) with `LlmEventSummarizer`.
   - Bundled `app/data/fsi_use_cases.db` (145 verified `Deployed - Field Ready` use cases across 7 verticals).
3. **Orchestration & Logic (`app/config.py`, `app/agent.py`)**:
   - Programmatic HITL gate (`enforce_hitl_gate_before_tool`) blocking same-turn auto-approval and unapproved downstream execution.
   - Explicit `SequentialAgent` (`rpd_sequential_pipeline`), `LoopAgent` (`rpd_value_validation_loop`), and `EscalationChecker(BaseAgent)` workflows with `output_key` state routing.
4. **Observability & Tracing (`app/telemetry.py`, `app/fast_api_app.py`)**:
   - OpenTelemetry `TracerProvider` and custom spans (`rpd.match_and_score_use_cases`, `rpd.extract_verified_value_and_kpis`, `rpd.synthesize_transformation_report`).
   - Google Cloud Structured JSON audit logs (`SSRF_URL_BLOCKED`, `HARD_GATE_FILTER`, `HITL_DECISION`, `UNGROUNDED_KPI_BLOCKED`, `REPORT_SYNTHESIZED`) with trace correlation and `/healthz` readiness probe.
5. **Infrastructure & CI/CD (`Dockerfile`, `.antigravity/pipeline.yaml`, `rpd_eval_set.json`)**:
   - Multi-stage non-root `Dockerfile` (`uid 10001`), 4-case trajectory & adversarial evaluation suite (`rpd_eval_set.json`), and automated CI/CD pipelines.

## Multi-Agent Topology

1. **Agent 1 (`app/agent_discovery.py`) — Discovery & Industry Classifier:** Ingests up to 5 Web URLs and up to 5 discovery documents (with HTTP 403/429 fallback to raw text) and outputs `CustomerContextSchema`.
2. **Agent 2 (`app/agent_matcher.py` & `app/tools/matcher.py`) — Deterministic Repository Matcher & Ranker:** Queries the unified `use_cases` table in `/usr/local/google/home/nevardasmith/fsi_use_cases.db`, enforces **Hard Gate 1** (`Deployed - Field Ready`) and **Hard Gate 2** (verified `Customer Reference`), applies weighted scoring (`+40` Demo Link, `+30` Capabilities Link, `+30` Semantic Fit), and outputs `RankedUseCasePool`.
3. **HITL Gateway (`app/agent_hitl.py`) — Solution Curator:** Pauses execution for AI Transformation Manager review and persists session checkpoints via `add_session_to_memory_callback`, outputting `ApprovedUseCases`.
4. **Agent 3 (`app/agent_value.py`) — Value Driver & KPI Engine:** Enforces the **Zero Hallucination Rule**, binding realized KPIs strictly to verified customer references into `ValueAnalysisDossier`.
5. **Agent 4 (`app/agent_synthesizer.py` & `app/agent.py`) — Executive Opportunity Synthesizer:** Synthesizes executive proposals adhering strictly to `TransformationReportItem`.

## SPIFFE Agent Identity Deployment & IAM Authorization

### 1. Deploy with Cryptographic SPIFFE Agent Identity
```bash
agents-cli deploy --agent-identity --region us-central1
```

### 2. Grant Least-Privilege IAM Role (`roles/datastore.user`) to the SPIFFE Principal ID
Once deployed, bind `roles/datastore.user` to the provisioned SPIFFE Principal ID:
```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="principal://agents.global.proj-12345.system.id.goog/resources/aiplatform/projects/12345/locations/us-central1/reasoningEngines/67890" \
  --role="roles/datastore.user"
```
*(Replace `12345` with your project number and `67890` with the deployed Reasoning Engine ID output by `agents-cli deploy`.)*
