# Agent 2 — Candidate Indexing: Implemented Flow & Testing Guide

> Grounded in the actual implementation as of 2026-04-26.
> Reference files: `apps/backend/app/temporal/product/candidate_indexing/`

---

## Trigger Paths

Two paths fire `CandidateIndexingWorkflow`:

| Path | Who | File | Payload |
|---|---|---|---|
| **API upload** | Recruiter JWT | `app/api/v1/endpoints/candidates.py` | PDF/DOCX upload → base64 → workflow |
| **Seed script** | CLI | `scripts/seed_candidates.py` | JSON fixture → Markdown render → base64 → workflow |

Both produce identical `CandidateIndexingInput`:
```python
CandidateIndexingInput(
    raw_bytes_b64: str,   # base64 file bytes
    mime_type: str,       # application/pdf | text/markdown | ...
    source: str,          # "recruiter_upload" | "seed"
    source_recruiter_id: str | None,
)
```

---

## Workflow Execution Phases

The workflow tracks phase in `self._phase` (readable via `get_status` query at any point):

```
initialized → parsing_resume → fetching_github → inferring_skill_depth
           → resolving_duplicates → generating_embedding → persisting
           → indexing_graph → scoring_completeness → completed
```

---

## Step-by-Step Activity Chain

### Step 1 — `parse_resume`
**File:** `activities/parse_resume.py`  
**Type:** LLM | **Timeout:** 90s | **Retry:** max=3, backoff=2.0×

**Input:** `raw_bytes_b64: str`, `mime_type: str`

**What happens:**
1. Base64-decode bytes → `raw_bytes`
2. If `mime_type` is `text/markdown` or `text/plain` → skip docling, decode UTF-8 directly
3. Otherwise → write to `tempfile` → `DocumentConverter().convert(path)` → `export_to_markdown()`
4. LLM `structured_complete(markdown, schema=CandidateProfile)` with system prompt:
   > "Extract candidate profile. Set depth=claimed_only for ALL skills. Empty fields as null."
5. Backfill `resume_text = markdown` if LLM omitted it (needed for downstream citation resolution)

**Output:** `CandidateProfile` dict — `{full_name, email, github_username, skills: [{name, depth="claimed_only"}], work_history, education, seniority, ...}`

---

### Step 2 — `fetch_github_signals`
**File:** `activities/fetch_github_signals.py`  
**Type:** External API | **Timeout:** 60s | **Retry:** max=5, initial=2s, backoff=2.0×, max_interval=120s

**Input:** `github_username: str | None`

**What happens:**
- If `github_username` is `None` or empty → return empty `GitHubSignals` immediately (no network call)
- Call `GitHubClient.fetch_user_signals(username)`:
  - `GET /users/{username}` → `public_repos` count
  - `GET /users/{username}/repos?sort=updated&per_page=30` → language map + stars
  - `GET /users/{username}/events/public?per_page=100` → count `PushEvent` for `commits_12m` estimate
- `GitHubNotFound` (404) → log warning, return empty `GitHubSignals` (not an error)
- `GitHubRateLimited` (429/403) → re-raise → Temporal retries with backoff

**Output:** `GitHubSignals` dict — `{repo_count, top_language, commits_12m, stars_total, languages: {lang: count}}`

---

### Step 3 — `infer_skill_depth`
**File:** `activities/infer_skill_depth.py`  
**Type:** LLM | **Timeout:** 60s | **Retry:** max=3, backoff=2.0×

**Input:** `profile_data: dict`, `github_signals_data: dict`

**What happens:**
- If `github.is_empty()` or no skills → return profile unchanged (all skills stay `claimed_only`)
- Otherwise → LLM call with rule:
  - Skill in `github.languages` + `commits_12m > 100` → `evidenced_commits`
  - Skill in `github.languages` only → `evidenced_projects`
  - Skill not in `github.languages` → `claimed_only`
- Parse LLM JSON response (strips markdown fences if present)
- If returned skill count ≠ input count → fallback to original (no crash)
- If JSON parse fails → fallback to original

**Output:** Updated `CandidateProfile` dict with `skills[].depth` re-tagged

---

### Step 4 — `resolve_entity_duplicates`
**File:** `activities/resolve_entity_duplicates.py`  
**Type:** DB read-only | **Timeout:** 30s | **Retry:** max=3, backoff=1.5×

**Input:** `profile_data: dict`

**What happens:**
1. Compute `dedup_hash = sha256(lower(name) + normalized(email))`
2. `CandidateRepository.get_by_dedup_hash(dedup_hash)` → PG lookup
3. If miss AND `github_username` present → `CandidateRepository.get_by_github_username(username)`
4. Return result with `is_duplicate`, `existing_candidate_id`, `match_source`

**Dedup priority:** `dedup_hash` checked first, `github_username` only as fallback.

**Output:** `{is_duplicate: bool, existing_candidate_id: str|None, match_source: "dedup_hash"|"github_username"|None}`

**No writes — safe for Temporal replay.**

---

### Step 5 — `generate_embedding`
**File:** `activities/generate_embedding.py`  
**Type:** Local ML | **Timeout:** 30s | **Retry:** max=2, backoff=1.5×

**Input:** `profile_data: dict`

**What happens:**
- Compose enriched text: `"{name} | {seniority} | {skills_csv} | {work_history_summary} | {location}"`
- `asyncio.to_thread(model.encode, text, normalize_embeddings=True)` — runs in thread executor, never blocks event loop
- Model: `all-MiniLM-L6-v2`, lazy-loaded singleton in worker process

**Output:** `{embedding: [float × 384]}`

---

### Step 6 — `persist_candidate_record`
**File:** `activities/persist_candidate_record.py`  
**Type:** DB write | **Timeout:** 30s | **Retry:** max=3, backoff=1.5×

**Input:** `profile_data, embedding, github_signals, source, source_recruiter_id, existing_candidate_id`

**What happens:**

**UPDATE path** (if `existing_candidate_id` provided):
- Fetch row by UUID
- Merge non-null new fields over existing (never overwrites with null)
- Update `embedding`, `github_signals`, `updated_at`
- Returns `{candidate_id: existing_id, was_insert: False}`

**INSERT path** (new candidate):
- Compute `dedup_hash = sha256(lower(name) + normalized(email))`
- Insert `Candidate` row with `status="indexing"`, `completeness_score=0`
- Writes `embedding` as pgvector `Vector(384)`
- Returns `{candidate_id: new_uuid, was_insert: True}`

**Why sequenced before Step 7:** The `candidate_id` returned here is passed to Neo4j indexing so the Neo4j `Candidate` node gets the real PG UUID (not a placeholder).

---

### Step 7 — `index_candidate_to_graph`
**File:** `activities/index_candidate_to_graph.py`  
**Type:** Neo4j write | **Timeout:** 30s | **Retry:** max=3, backoff=1.5×

**Input:** `candidate_id: str`, `profile_data: dict`, `github_signals_data: dict`

**What happens (all MERGE — idempotent):**

```cypher
-- Candidate node
MERGE (c:Candidate {id: $candidate_id})
SET c.name = $name

-- Per work_history entry
MERGE (co:Company {name: $company_name})
MERGE (c:Candidate {id: $id})-[:WORKED_AT {role_title, start_date, end_date}]->(co)

-- Per skill
MERGE (t:Technology {name: $skill_name})
MERGE (c:Candidate {id: $id})-[:SKILLED_IN {depth: $depth}]->(t)

-- If github_username present
MERGE (g:GitHubProfile {username: $username})
SET g.repo_count, g.top_language, g.commits_12m, g.stars_total
MERGE (c:Candidate {id: $id})-[:HAS_GITHUB]->(g)

-- Seniority enum
MERGE (s:SeniorityEnum {level: $seniority})
MERGE (c:Candidate {id: $id})-[:SENIORITY]->(s)

-- Per stage_fit entry
MERGE (st:StageEnum {stage: $stage})
MERGE (c:Candidate {id: $id})-[:FITS_STAGE]->(st)
```

Running twice with same input → identical graph state. Safe for Temporal crash recovery.

**Output:** `{nodes_merged: int, edges_merged: int}`

---

### Step 8 — `score_profile_completeness`
**File:** `activities/score_profile_completeness.py`  
**Type:** Deterministic + DB write | **Timeout:** 30s | **Retry:** max=3, backoff=1.5×

**Input:** `candidate_id: str`, `profile_data: dict`, `github_signals_data: dict`

**Scoring weights (sum = 1.0):**

| Field | Weight | Condition |
|---|---|---|
| `full_name` | 0.05 | present |
| `email` | 0.05 | present |
| `seniority` | 0.10 | present |
| `years_experience` | 0.05 | present |
| `skills` | 0.15 | ≥ 3 skills |
| `work_history` | 0.15 | ≥ 1 entry |
| `education` | 0.05 | present |
| `github_username + signals` | 0.20 | username + non-empty signals |
| `resume_text` | 0.10 | present |
| `location` | 0.05 | present |
| `stage_fit` | 0.05 | present |

**Status logic:**
- `score >= 0.5` → `status = "indexed"`
- `score < 0.5` → `status = "review_queue"` (candidate needs more data)

**DB write:** Updates `Candidate.completeness_score` and `Candidate.status` in PG.

**Output:** `{completeness_score: float, status: str, review_required: bool}`

---

## Workflow Return Value

```python
IndexingResult(
    candidate_id: str,         # PG UUID
    status: "indexed" | "review_queue",
    completeness_score: float, # 0.0 – 1.0
    was_duplicate: bool,
    source: "seed" | "recruiter_upload",
)
```

---

## Data Written Per Execution

| Store | What | Key |
|---|---|---|
| **PostgreSQL `candidates`** | One row per unique person | `dedup_hash` (unique) |
| **PostgreSQL `candidates`** | `embedding Vector(384)` | same row |
| **Neo4j** | `Candidate` node + `Company`, `Technology`, `GitHubProfile`, `SeniorityEnum`, `StageEnum` nodes + edges | `Candidate.id` = PG UUID |

---

## Testing Steps

### Prerequisites

```bash
# Terminal 1 — start infrastructure
cd /Users/omkar.gade/Desktop/Personel/shreekar/converio
docker compose up -d

# Verify services up
docker compose ps
# Expect: postgres, neo4j, temporal, temporal-ui all "running"

# Terminal 2 — pull local LLM (one-time, ~4.5GB)
ollama pull qwen2.5:7b
ollama serve  # if not already running as daemon
```

```bash
# Terminal 3 — backend setup
cd /Users/omkar.gade/Desktop/Personel/shreekar/converio/apps/backend
cp .env.example .env
uv sync --extra dev
```

---

### Test 1: Unit + integration tests (no Temporal server needed)

```bash
cd apps/backend

# All deterministic activity tests (real PG + Neo4j)
uv run pytest tests/temporal/activities/ -v

# Expected output:
# test_score_profile_completeness.py::test_compute_score_parametrized[...] PASSED (x5+)
# test_resolve_entity_duplicates.py::test_no_duplicate PASSED
# test_resolve_entity_duplicates.py::test_duplicate_via_dedup_hash PASSED
# test_persist_candidate_record.py::test_insert_new PASSED
# test_persist_candidate_record.py::test_upsert_idempotent PASSED
# test_generate_embedding.py::test_embedding_dim PASSED
# test_index_candidate_to_graph.py::test_idempotent_merge PASSED
# ...

# GitHub + LLM-stub tests (no services needed)
uv run pytest tests/core/ tests/temporal/activities/test_fetch_github_signals.py tests/temporal/activities/test_parse_resume.py tests/temporal/activities/test_infer_skill_depth.py -v
```

---

### Test 2: Workflow integration tests (Temporal time-skipping, no server)

```bash
uv run pytest tests/temporal/test_candidate_indexing_workflow.py -v

# Expected:
# test_happy_path_candidate_indexed PASSED
# test_duplicate_candidate_was_duplicate_true PASSED
# test_low_completeness_routes_to_review_queue PASSED
```

These use `WorkflowEnvironment.start_time_skipping()` — no Temporal server required.

---

### Test 3: Seed 100 candidates end-to-end

Requires: Temporal worker running + all services up.

```bash
# Terminal 4 — start worker
cd apps/backend
uv run python -m app.temporal.worker
# Expected log: "Starting 1 workers" + "CandidateIndexingWorkflow" registered

# Terminal 5 — start API
uv run python -m app.main
# Expected log: "Database initialized successfully" + "Neo4j initialized successfully"

# Terminal 6 — run seed
uv run python scripts/seed_candidates.py --limit 5  # start with 5 to verify
# Expected:
#   [1/5] Started: Alice Chen
#   [2/5] Started: Bob Martinez
#   ...
#   Done. Started: 5, Skipped (idempotent): 0

# Monitor in Temporal UI
open http://localhost:8080
# Navigate to Workflows → filter "CandidateIndexingWorkflow" → watch phase transitions
```

**Verify PG rows:**
```bash
docker exec -it converio-postgres-1 psql -U converio -d converio -c \
  "SELECT full_name, status, completeness_score, github_username FROM candidates ORDER BY created_at DESC LIMIT 10;"

# Expected:
#  full_name        | status  | completeness_score | github_username
# ------------------+---------+--------------------+-----------------
#  Alice Chen       | indexed | 0.92               | alice-chen
#  Bob Martinez     | indexed | 0.75               | null
#  ...
```

**Verify Neo4j graph:**
```bash
# Open Neo4j browser
open http://localhost:7474
# Username: neo4j / Password: neo4j_converio (from .env)

# Run this Cypher to verify graph for first candidate:
# MATCH (c:Candidate)-[r]->(n) RETURN c, r, n LIMIT 50
```

**Re-run seed (idempotency test):**
```bash
uv run python scripts/seed_candidates.py --limit 5
# Expected: "Skipped (idempotent): 5" — no duplicates created
```

---

### Test 4: Single resume upload via API

```bash
# Requires a test PDF resume — create a minimal one
echo "John Smith, Senior Engineer, Python, Kafka, AWS. Worked at Stripe 2020-2023." > /tmp/test_resume.txt

# Note: Auth required. For local dev, get a JWT from Supabase or bypass auth middleware.
# If auth middleware is in place, first get a token:
# TOKEN=$(curl -s -X POST http://localhost:8000/auth/login -d '{"email":"test@example.com","password":"test"}' | jq -r .token)

# Upload (replace TOKEN with actual JWT or temporarily disable auth for testing)
curl -X POST http://localhost:8000/api/v1/candidates/index \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/test_resume.txt;type=text/plain" \
  -v

# Expected response (202 Accepted):
# {
#   "status": true,
#   "message": "Resume uploaded — indexing started",
#   "data": {
#     "workflow_id": "candidate-indexing-<uuid>",
#     "filename": "test_resume.txt"
#   }
# }

# Poll workflow status via Temporal query
# (or watch Temporal UI at http://localhost:8080)
```

**Check MIME type validation (expect 415):**
```bash
curl -X POST http://localhost:8000/api/v1/candidates/index \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/malware.exe;type=application/octet-stream" \
  -v
# Expected: HTTP 415 Unsupported Media Type
```

**Check size limit (expect 413):**
```bash
dd if=/dev/zero of=/tmp/big.pdf bs=1M count=6
curl -X POST http://localhost:8000/api/v1/candidates/index \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/big.pdf;type=application/pdf" \
  -v
# Expected: HTTP 413 Request Entity Too Large
```

---

### Test 5: Temporal crash recovery (durability test)

```bash
# Start worker, seed 1 candidate
uv run python scripts/seed_candidates.py --limit 1

# Immediately kill the worker mid-execution
kill -9 $(pgrep -f "app.temporal.worker")

# Restart worker
uv run python -m app.temporal.worker

# Watch Temporal UI — workflow resumes from last completed activity
# The already-completed steps are NOT re-executed (Temporal event history)
# Expected: workflow completes successfully
```

---

### Test 6: LLM provider swap

```bash
# Switch from Ollama to OpenRouter with zero code change
OPENROUTER_API_KEY=sk-or-xxx
LLM_PROVIDER=openrouter

# Restart worker
uv run python -m app.temporal.worker

# Run seed — same behavior, faster (~3s/call vs ~15s/call with Ollama)
uv run python scripts/seed_candidates.py --limit 3
```

---

### Test 7: Query live workflow status

```bash
# While a workflow is running, query its current phase
temporal workflow query \
  --workflow-id "seed-candidate-alice-chen-0" \
  --query-type "get_status" \
  --namespace default

# Expected output:
# {
#   "phase": "fetching_github",
#   "candidate_id": null,
#   "completeness_score": null
# }

# After completion:
# {
#   "phase": "completed",
#   "candidate_id": "550e8400-e29b-41d4-a716-446655440000",
#   "completeness_score": 0.87
# }
```

---

## Completeness Score Reference

| Scenario | Score | Status |
|---|---|---|
| All fields + GitHub with activity | ~1.0 | `indexed` |
| Good profile, no GitHub | ~0.75 | `indexed` |
| Name + email + 3 skills + 1 job | ~0.55 | `indexed` |
| Name only | 0.05 | `review_queue` |
| Name + email + seniority | 0.20 | `review_queue` |

Threshold: `< 0.5` → `review_queue`. GitHub (0.20 weight) is the biggest single factor.

---

## Known Limitations (v1)

1. **`review_queue` has no UI surface yet** — `Candidate.status="review_queue"` is set but there's no operator review screen. Candidates queue silently.
2. **Neo4j `Company` nodes not deduplicated globally** — two candidates who both worked at "Stripe" create two separate `:Company {name: "Stripe"}` nodes unless MERGE collapses them (it will, since MERGE on `name` is idempotent).
3. **`commits_12m` is an estimate** — GitHub public events API returns ≤300 events; high-volume committers are underestimated.
4. **5MB upload cap** — Temporal default gRPC payload limit is ~4MB after base64 encoding overhead. Resumes near 3.5MB pre-encoding are safe; larger resumes need external blob storage (S3) with a URL reference instead of inline bytes.
5. **Ollama JSON-mode flakiness** — `infer_skill_depth` falls back to original skills on LLM parse failure. `parse_resume` relies on Temporal retry (max=3) if Pydantic validation fails.

---

*Document generated 2026-04-26. Grounded in implemented code at `apps/backend/app/temporal/product/candidate_indexing/`.*
