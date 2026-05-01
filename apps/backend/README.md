# Converio Backend

AI-native talent matching engine — FastAPI + Temporal + Neo4j + PostgreSQL.

## Agent 2 — Candidate Indexing Workflow

### Prerequisites

- Docker Desktop running
- `ollama` installed (https://ollama.ai) — local LLM for dev
- Python 3.12 + `uv` installed

### 1. Start infrastructure

```bash
# From repo root
docker compose up -d
```

Starts: PostgreSQL (port 5432), Neo4j (port 7474/7687), Temporal (port 7233).

### 2. Pull the local LLM model

```bash
ollama pull qwen2.5:7b
```

`qwen2.5:7b` is the default. Strongest JSON-mode adherence in 7B class. Requires ~4.5GB disk.

### 3. Configure environment

```bash
cp .env.example .env
```

Key settings for local dev (already set as defaults in `.env.example`):
```
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
GITHUB_TOKEN=<optional — increases rate limit from 60 to 5000 req/hr>
```

To use OpenRouter instead of Ollama:
```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=<your key>
```

### 4. Install dependencies

```bash
uv sync --extra dev
```

### 5. Run the Temporal worker

```bash
# Terminal 1
uv run python -m app.temporal.worker
```

The worker registers `CandidateIndexingWorkflow` + 8 activities and polls `converio-queue`.

### 6. Start the API server

```bash
# Terminal 2
uv run python -m app.main
```

Runs migrations + Neo4j constraint setup on first start.

### 7. Seed 100 synthetic candidates

```bash
# Terminal 3
uv run python scripts/seed_candidates.py --limit 100
```

- Fires one `CandidateIndexingWorkflow` per candidate
- Skips already-indexed candidates (idempotent via deterministic workflow IDs)
- Monitor progress at http://localhost:8080 (Temporal Web UI)

### 8. Test single resume upload

```bash
curl -X POST http://localhost:8000/api/v1/candidates/index \
  -H "Authorization: Bearer <JWT>" \
  -F "file=@/path/to/resume.pdf"
```

Returns `202 Accepted` with `{"workflow_id": "candidate-indexing-<uuid>"}`.

### Running tests

```bash
# Unit + integration tests (requires Docker Compose services running)
uv run pytest tests/ -v

# Skip Neo4j-dependent tests if Neo4j unavailable
uv run pytest tests/ -v -k "not neo4j"

# Ollama smoke test (requires Ollama running with model pulled)
uv run pytest tests/llm/ -v -m ollama
```

### Switching LLM providers

All activities use `get_llm_client()` which reads `LLM_PROVIDER` from env at startup.

| Provider | Env | Speed | Cost |
|---|---|---|---|
| `ollama` | `OLLAMA_HOST`, `OLLAMA_MODEL` | 5–30s/call | Free |
| `openrouter` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | 1–3s/call | ~$0.001/call |
| `gemini` | `GEMINI_API_KEY`, `GEMINI_MODEL` | 1–3s/call | Free tier |

Change `LLM_PROVIDER` and restart worker — zero code change required.

### Architecture

```
POST /api/v1/candidates/index (file upload)
    ↓
CandidateIndexingWorkflow (Temporal)
    parse_resume (docling → Markdown → LLM CandidateProfile)
    fetch_github_signals (GitHub REST API, retry=5)
    infer_skill_depth (LLM re-tags skills with GitHub evidence)
    resolve_entity_duplicates (PG dedup_hash + Neo4j github lookup)
    generate_embedding (sentence-transformers all-MiniLM-L6-v2, 384-dim)
    persist_candidate_record (PostgreSQL upsert)
    index_candidate_to_graph (Neo4j MERGE — Candidate, Company, Technology, GitHubProfile)
    score_profile_completeness (deterministic, flips status=review_queue if <0.5)
    ↓
Candidate row in PostgreSQL + subgraph in Neo4j
```

---

## Agent 1 — Job Intake Workflow

Root Temporal workflow for every role Converio takes on. Companies submit a managed intake; the workflow classifies the role and generates a weighted evaluation rubric. The pipeline exits with `jobs.status="recruiter_assignment"`, ready for Agent 0 to pick up in a follow-up PR.

### End-to-end smoke

Prereqs: same Docker stack as Agent 2 (PostgreSQL + Neo4j + Temporal) plus the worker running.

```bash
# 1. Seed operators + companies (idempotent re-runs)
uv run python scripts/seed_operators.py
uv run python scripts/seed_companies.py

# 2. As an operator, create a company + provision a hiring-manager seat
curl -X POST http://localhost:8000/api/v1/companies \
  -H "Authorization: Bearer $OPERATOR_JWT" \
  -d '{"name":"Lattice Labs","stage":"series_a"}'
# → 201 with {id, name, ...}

curl -X POST http://localhost:8000/api/v1/companies/$COMPANY_ID/users \
  -H "Authorization: Bearer $OPERATOR_JWT" \
  -d '{"email":"hm@lattice.example.com","role":"hiring_manager"}'
# → 201

# 3. Submit an intake (operator on behalf of, or hiring manager once signed in)
curl -X POST http://localhost:8000/api/v1/jobs/intake \
  -H "Authorization: Bearer $JWT" \
  -d '{"company_id":"'"$COMPANY_ID"'","title":"Founding Engineer",
       "jd_text":"...full JD...","intake_notes":"small team, generalist preferred"}'
# → 202 with {job_id, workflow_id, status: "intake"}

# 4. Watch the workflow in Temporal Web UI (http://localhost:8080)
#    JobIntakeWorkflow → status: completed
#    jobs.status flips intake → recruiter_assignment
```

### Architecture

```
POST /api/v1/jobs/intake (managed intake payload)
    ↓
INSERT jobs row (status=intake, workflow_id=job-intake-<uuid>)
    ↓
JobIntakeWorkflow (Temporal, REJECT_DUPLICATE)
    classify_role_type (LLM structured output → RoleClassification)
    generate_evaluation_rubric (LLM raw JSON → 4-8 weighted dimensions, normalized)
    persist_job_record (UPDATE jobs + INSERT rubric v1 + UPSERT workflow_runs)
    [TODO Agent 0: execute_child_workflow RecruiterAssignmentWorkflow]
    ↓
jobs.status = recruiter_assignment + rubrics row v1 in PostgreSQL
```

### Operational notes

- **Auth on `/jobs/intake`**: dual path — Supabase JWT resolves to either an `operators` row (operator-on-behalf-of) or a `company_users` row (hiring-manager self-service). Company users can only intake roles for their own `company_id`.
- **Rate limit**: 10 intakes per hour per `company_id`, in-process sliding window. Single-process only — multi-worker / multi-pod deployments compound this ceiling and reset on restart. Swap for Redis-backed limiter before production scale.
- **Workflow ID convention**: `job-intake-<job_id>`. `WorkflowIDReusePolicy.REJECT_DUPLICATE` — re-intake is treated as a bug; rubric reeval flows through HITL #2 (separate PR).
- **Logging**: `jd_text` and `intake_notes` are never logged. Log lines carry `job_id`, `workflow_id`, `company_id`, `actor_kind`, `actor_id` only.
- **Out of scope this PR**: Agents 0/3/4/5/6, both HITL Signal handlers, frontend portals, ATS push, Slack notifications. See `docs/plans/job_intake_plan.md` for the full out-of-scope list.
