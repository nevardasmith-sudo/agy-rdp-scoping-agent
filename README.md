# agy-rpd-scoping-agent

Repeatable, Portable, and Demoable (RPD) Transformation Engine built on the Google Agent Development Kit (ADK) for the Gemini Enterprise Agent Platform.

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
