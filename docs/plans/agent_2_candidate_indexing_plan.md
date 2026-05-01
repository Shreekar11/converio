# Agent 2 — Candidate Indexing Workflow: Implementation Plan

> Durable handoff document for Converio Match proof-of-work. Captures locked decisions, pre-plan defaults, phased task breakdown, and acceptance criteria for Agent 2 (Candidate Indexing Workflow). Future subagents implementing Agent 2 should treat this as the source of truth alongside `docs/contrario_proof_of_work_context.md` §9 + §17 and `docs/conventions/backend-patterns.md`.
>
> **Status:** Plan approved, pre-Phase-A.
> **Owner:** Shreekar
> **Last updated:** 2026-04-26

---

## 0. Source Context

- Master design doc: `docs/contrario_proof_of_work_context.md`
  - §9 — Phase 1 — Candidate Indexing Workflow activity chain
  - §17 — Tool Calls Per Agent (Agent 2 entry)
  - §19 — Key Architectural Decisions
- Backend conventions: `docs/conventions/backend-patterns.md`
- Data models already implemented: `apps/backend/app/database/models.py` (`Candidate`, `Recruiter`, `WorkflowRun`, `HitlEvent`, etc.)
- Repositories already wired: `apps/backend/app/repositories/candidates.py` (`get_by_dedup_hash`, `get_by_github_username`)
- Neo4j client: `apps/backend/app/core/neo4j_client.py` (composite `(id, workflow_id)` constraints — needs refactor in A9)
- Temporal scaffold: `apps/backend/app/temporal/{core,worker.py}` ready; `app/temporal/product/` empty awaiting candidate_indexing module.

## 1. Locked Decisions (from prior conversation turns)

| # | Decision |
|---|---|
| 1 | Resume parsing: **docling** (deterministic, layout-aware, PDF + DOCX). Replaces LLM-only parse. LLM still used downstream for structured extraction from Markdown output. |
| 2 | LLM client: **Strategy pattern** — three concrete strategies (OpenRouter, Gemini direct, Ollama). Factory singleton via `get_llm_client()`. Default `LLM_PROVIDER=ollama` in dev for token savings; flip via env to `openrouter` in prod. |
| 3 | Embeddings: local `sentence-transformers all-MiniLM-L6-v2`, 384-dim. Lazy singleton per worker process. |
| 4 | Workflow IO: JSON-serializable Pydantic models, IDs as strings. No ORM objects across activity boundaries. |
| 5 | Idempotency: PG upsert keyed on `dedup_hash` (`sha256(lower(name) + normalized(email))`). Neo4j MERGE for all node/edge writes. |

## 2. Pre-Plan Defaults (locked at plan creation)

Open questions from initial context pack §8 — locked with recommended defaults so plan can execute without further blocking input. Override during execution if needed.

| # | Question | Recommended Default |
|---|---|---|
| Q1 | Submission linkage — workflow vs API caller | **API caller** creates `candidate_submissions` row; workflow handles only `candidates` |
| Q2 | GitHub username missing | Graceful skip step 2 + skill-depth defaults to `claimed_only` |
| Q3 | Workflow return shape | `{candidate_id, status, completeness_score, was_duplicate, dimensions_scored}` |
| Q4 | Completeness < 0.5 action | Insert `hitl_events` row + flip `Candidate.status=review_queue`; no parent Signal (workflow may run outside `JobIntakeWorkflow` context — e.g., seeder) |
| Q5 | Neo4j composite constraint `(id, workflow_id)` | **Refactor**: global entities (`Candidate`, `Company`, `Technology`, `GitHubProfile`, `Recruiter`, `Domain`, `CompanyStage`, `Metric`, `StageEnum`, `SeniorityEnum`) get `(id) UNIQUE`. Per-job entity `Job` keeps `(id, workflow_id) UNIQUE`. Migration in Phase A (A9). |
| Q6 | Retry policies | LLM activities: max=3, backoff=2.0 / `fetch_github_signals`: max=5, backoff=2.0, max_interval=120s / DB writes: max=3, backoff=1.5 / Embedding: max=2 (deterministic, fail fast) |
| Q7 | Seed dataset | `tests/fixtures/seed_candidates.json` — 100 synthetic profiles; CLI `scripts/seed_candidates.py` |
| Q8 | API auth on `/candidates/index` | Required (recruiter JWT). Source recruiter ID derived from `current_user` → `recruiters.supabase_user_id` lookup. |

---

## 3. Phase A — Infrastructure Gap Fill (no business logic)

**Goal:** stand up every dependency activities will need. Each task isolated, unit-testable, no Temporal involvement.

### A1. Add deps to `pyproject.toml`

| Field | Value |
|---|---|
| Goal | Pin new third-party libs |
| Files | `apps/backend/pyproject.toml`, `apps/backend/requirements.txt`, regenerate `uv.lock` |
| Output | Add `docling>=2.0.0`. Confirm already pinned: `httpx`, `aiohttp`, `google-genai`, `sentence-transformers`, `temporalio`, `neo4j`, `pgvector`, `pydantic>=2.5`. |
| Acceptance | `uv sync` succeeds in clean venv. `python -c "import docling, sentence_transformers, neo4j, temporalio"` exits 0. |

### A2. Extend `LLMSettings` + `.env.example`

| Field | Value |
|---|---|
| Goal | Provider-aware config for 3 LLM strategies |
| Files | `apps/backend/app/core/config.py`, `apps/backend/.env.example` |
| Output | Fields: `provider`, `openrouter_api_key/url/model`, `gemini_api_key/model`, `ollama_host/model`. Defaults: `provider=ollama`, `ollama_host=http://localhost:11434`, `ollama_model=qwen2.5:7b`. |
| Acceptance | `from app.core.config import settings; settings.llm.provider == "ollama"` in fresh env. |

### A3. Build LLM Strategy module

| Field | Value |
|---|---|
| Goal | Provider-agnostic LLM interface, swap via env, no caller code change between providers |
| Files | `apps/backend/app/core/llm/{__init__.py, base.py, openrouter_client.py, gemini_client.py, ollama_client.py, factory.py}` |
| Output | `LLMClient` ABC with `complete()`, `structured_complete()`, `close()`. Three concrete strategies. `get_llm_client()` process-wide singleton dispatched by `settings.llm.provider`. `LLMMessage`, `LLMResponse` Pydantic models. |
| Acceptance | Integration smoke test: `await get_llm_client().structured_complete([msg], TinySchema)` returns valid `TinySchema` instance against running Ollama at `localhost:11434`. |

### A4. Build Temporal client factory

| Field | Value |
|---|---|
| Goal | Reusable `Client.connect()` for trigger sites (API endpoints, seeder script) |
| Files | `apps/backend/app/core/temporal_client.py` |
| Output | `async def get_temporal_client() -> Client` — process-wide singleton, reads `settings.temporal`. `async def close_temporal_client()` for lifespan teardown. |
| Acceptance | `await get_temporal_client()` connects to running Temporal at `localhost:7233`. |

### A5. Build embeddings singleton

| Field | Value |
|---|---|
| Goal | Lazy-load sentence-transformers once per worker process |
| Files | `apps/backend/app/core/embeddings.py` |
| Output | `def get_embedding_model() -> SentenceTransformer` (sync, lazy singleton). `async def embed_text(text: str) -> list[float]` returns 384-dim list. Blocking model invocation wrapped in `asyncio.to_thread`. |
| Acceptance | `len(await embed_text("hello")) == 384`. Second call < 50ms (model cached in memory). |

### A6. Build GitHub client

| Field | Value |
|---|---|
| Goal | Async REST client w/ rate-limit awareness |
| Files | `apps/backend/app/core/github_client.py` |
| Output | `class GitHubClient` with `async fetch_user_signals(username) -> GitHubSignals` returning `{repo_count, top_language, commits_12m, stars_total, languages: dict[str, int]}`. Reads `GITHUB_TOKEN` env (optional — unauth has lower rate limits). Honors `X-RateLimit-Remaining` and `Retry-After`. Raises typed errors: `GitHubNotFound`, `GitHubRateLimited(retry_after: int)`. |
| Acceptance | Mocked httpx test: 200 response → populated `GitHubSignals`. 404 → `GitHubNotFound`. 429 + `Retry-After: 5` → raises `GitHubRateLimited(retry_after=5)`. |

### A7. Build docling parser wrapper

| Field | Value |
|---|---|
| Goal | Singleton `DocumentConverter`, deterministic resume → Markdown |
| Files | `apps/backend/app/core/document_parser.py` |
| Output | `def get_document_converter() -> DocumentConverter` (lazy singleton). `async def parse_document(raw_bytes: bytes, mime_type: str) -> str` returns Markdown. Blocking docling call wrapped in `asyncio.to_thread`. |
| Acceptance | Sample resume PDF in `tests/fixtures/sample_resume.pdf` parses to Markdown containing recognizable section headings (`## Experience`, `## Skills`, etc.). |

### A8. Pre-pull docling model in worker Dockerfile

| Field | Value |
|---|---|
| Goal | Avoid first-run model download in production worker |
| Files | `apps/backend/Dockerfile.worker` |
| Output | After deps install: `RUN python -c "from docling.document_converter import DocumentConverter; DocumentConverter()"` to materialize model weights into image layers. |
| Acceptance | `docker build -f Dockerfile.worker .` succeeds; image RSS at idle ~1GB; first activity invocation does not download anything. |

### A9. Refactor Neo4j constraints (Q5)

| Field | Value |
|---|---|
| Goal | Composite `(id, workflow_id)` constraint awkward for global entities like `Candidate` |
| Files | `apps/backend/app/core/neo4j_client.py`, optional `apps/backend/scripts/neo4j_migrate.py` |
| Output | Split labels: `GLOBAL_LABELS = ["Candidate", "Company", "Technology", "GitHubProfile", "StageEnum", "SeniorityEnum", "Recruiter", "Domain", "CompanyStage", "Metric"]` → `(id) UNIQUE`. `WORKFLOW_SCOPED_LABELS = ["Job"]` → `(id, workflow_id) UNIQUE`. `ensure_constraints()` drops legacy composites for global labels and creates new constraints idempotently. |
| Acceptance | `SHOW CONSTRAINTS` returns single-prop unique on `Candidate`, composite on `Job`. Re-running `ensure_constraints()` is idempotent (no errors, no duplicate constraints). |

### A10. Define product Pydantic schemas

| Field | Value |
|---|---|
| Goal | Type-safe activity IO |
| Files | `apps/backend/app/schemas/product/__init__.py`, `apps/backend/app/schemas/product/candidate.py` |
| Output | Models: `CandidateProfile`, `WorkHistoryItem`, `EducationItem`, `Skill` (`name: str` + `depth: Literal["claimed_only","evidenced_projects","evidenced_commits"]`), `GitHubSignals`, `IndexingResult`, `ResolveDuplicatesResult`, `CandidateIndexingInput`. All `BaseModel`, JSON-serializable. |
| Acceptance | `CandidateProfile.model_json_schema()` produces a valid JSON Schema usable by LLM `structured_complete` across all three providers. |

### A11. Lifespan wire-up

| Field | Value |
|---|---|
| Goal | Close LLM + Temporal clients on shutdown |
| Files | `apps/backend/app/main.py` |
| Output | Add `await close_llm_client()` + `await close_temporal_client()` to lifespan teardown block. |
| Acceptance | Uvicorn shutdown logs no resource-leak warnings; httpx clients explicitly closed. |

---

## 4. Phase B — Deterministic Activities (no LLM, no external API)

**Goal:** build everything testable without network. Foundation for LLM/external-API activities.

### B1. Activity: `generate_embedding`

| Field | Value |
|---|---|
| Goal | Profile text → 384-dim vector |
| Files | `apps/backend/app/temporal/product/candidate_indexing/activities/generate_embedding.py` |
| Input | `CandidateProfile` JSON dict |
| Output | `dict {embedding: list[float]}` (384 floats) |
| Logic | Compose enriched profile text: `f"{name} | {seniority} | {skills_csv} | {work_history_summary} | {github_top_lang}"`. Call `embed_text()` via `asyncio.to_thread`. |
| Registry | `@ActivityRegistry.register("candidate_indexing", "generate_embedding")` + `@activity.defn` |
| Acceptance | Unit test: deterministic vector for fixed input; `len == 384`. |

### B2. Activity: `resolve_entity_duplicates`

| Field | Value |
|---|---|
| Goal | PG dedup hash + Neo4j github_username lookup before insert |
| Files | `.../activities/resolve_entity_duplicates.py` |
| Input | `CandidateProfile` JSON |
| Output | `{is_duplicate: bool, existing_candidate_id: str | None, match_source: "dedup_hash" | "github_username" | None}` |
| Logic | Compute `dedup_hash = sha256(lower(name) + normalized(email))`. Call `CandidateRepository.get_by_dedup_hash`. If miss + has github_username → `get_by_github_username`. Open own session via `async_session_maker()`. |
| Acceptance | Insert candidate, run activity with same name+email → returns `is_duplicate=True`, correct `existing_candidate_id`. |

### B3. Activity: `persist_candidate_record`

| Field | Value |
|---|---|
| Goal | Upsert PG row |
| Files | `.../activities/persist_candidate_record.py` |
| Input | `CandidateProfile` JSON, `embedding: list[float]`, `github_signals: dict`, `source: str`, `source_recruiter_id: str | None`, `existing_candidate_id: str | None` |
| Output | `{candidate_id: str, was_insert: bool}` |
| Logic | If `existing_candidate_id` → UPDATE existing row (merge fields, prefer non-null new values). Else INSERT new with computed `dedup_hash`. Write `embedding` via pgvector. Set `status=indexing` (final status set by B4). |
| Acceptance | First call inserts; second call with same dedup_hash updates same row; embedding round-trips through pgvector unchanged. |

### B4. Activity: `score_profile_completeness`

| Field | Value |
|---|---|
| Goal | Compute 0–1 completeness score; flip status if low |
| Files | `.../activities/score_profile_completeness.py` |
| Input | `candidate_id: str`, `CandidateProfile` JSON, `GitHubSignals` JSON |
| Output | `{completeness_score: float, status: "indexed" \| "review_queue", review_required: bool}` |
| Logic | Weighted presence sum: name(0.05), email(0.05), seniority(0.10), years_exp(0.05), skills≥3(0.15), work_history≥1(0.15), education(0.05), github_username+signals(0.20), resume_text(0.10), location(0.05), stage_fit(0.05). Round to 2dp. If < 0.5: flip `Candidate.status=review_queue`. v1 skips `hitl_events` insert (see Risk note). Else `status=indexed`. |
| Risk | `hitl_events.actor_id` is NOT NULL but completeness review has no operator yet. **v1 decision:** skip event insert; status flip is sufficient signal. Revisit when building review UI — make `actor_id` nullable in a follow-up migration. |
| Acceptance | All-fields-present profile → score ≥ 0.9, status=indexed. Empty profile → < 0.5, status=review_queue. |

### B5. Activity: `index_candidate_to_graph`

| Field | Value |
|---|---|
| Goal | Idempotent Neo4j subgraph for candidate |
| Files | `.../activities/index_candidate_to_graph.py` |
| Input | `candidate_id: str`, `CandidateProfile` JSON, `GitHubSignals` JSON |
| Output | `{nodes_merged: int, edges_merged: int}` |
| Logic | Single Cypher transaction with MERGEs per master doc §7 schema: `Candidate(id)`, `Company(name)` per work_history entry → `(:Candidate)-[:WORKED_AT {start, end, role}]->(:Company)`, `Technology(name)` per skill → `(:Candidate)-[:SKILLED_IN {depth}]->(:Technology)`, `GitHubProfile(username)` if present → `(:Candidate)-[:HAS_GITHUB]->(:GitHubProfile)`, `(:Candidate)-[:SENIORITY]->(:SeniorityEnum {level})`, `(:Candidate)-[:FITS_STAGE]->(:StageEnum)` per stage_fit entry. |
| Acceptance | Run twice with same input → no duplicate nodes/edges. `MATCH (c:Candidate {id: ...})-[r]->(n) RETURN count(r)` stable across reruns. |

---

## 5. Phase C — LLM Activities

### C1. Activity: `parse_resume`

| Field | Value |
|---|---|
| Goal | Raw bytes → Markdown via docling, then LLM extracts structured profile |
| Files | `.../activities/parse_resume.py` |
| Input | `{raw_bytes_b64: str, mime_type: str}` |
| Output | `CandidateProfile` JSON dict (no `github_signals` yet — populated in D1) |
| Logic | Step 1: `parse_document(b64decode(raw_bytes), mime_type)` → Markdown. Step 2: `llm.structured_complete(messages=[system_prompt, user=markdown], schema=CandidateProfile)`. System prompt: "Extract candidate profile. Skills with `depth=claimed_only` (no GitHub evidence yet). Empty fields as null." |
| Retry | max=3, backoff=2.0 |
| Acceptance | Sample resume → CandidateProfile with name, ≥1 skill, ≥1 work_history entry. |

### C2. Activity: `infer_skill_depth`

| Field | Value |
|---|---|
| Goal | Re-tag skill depth using GitHub evidence |
| Files | `.../activities/infer_skill_depth.py` |
| Input | `CandidateProfile` JSON, `GitHubSignals` JSON |
| Output | Updated `CandidateProfile` JSON (skills tagged `claimed_only` / `evidenced_projects` / `evidenced_commits`) |
| Logic | If no GitHub → return profile unchanged (all `claimed_only`). Else: pass profile + signals to LLM, schema = `list[Skill]`. Prompt rule: skill name appears in `languages` dict + commit count > 100 → `evidenced_commits`; appears in `languages` only → `evidenced_projects`; else → `claimed_only`. |
| Retry | max=3, backoff=2.0 |
| Acceptance | Profile claims "Python" + GitHub languages includes Python with 500 commits → tagged `evidenced_commits`. |

---

## 6. Phase D — External API Activity

### D1. Activity: `fetch_github_signals`

| Field | Value |
|---|---|
| Goal | Pull GitHub signals; graceful no-op if username missing |
| Files | `.../activities/fetch_github_signals.py` |
| Input | `github_username: str | None` |
| Output | `GitHubSignals` JSON or empty `{}` if username null |
| Logic | If null → return `{}`. Else `GitHubClient.fetch_user_signals(username)`. Catch `GitHubNotFound` → log + return `{}`. Catch `GitHubRateLimited` → raise (Temporal retries with backoff). |
| Retry | max=5, initial=2s, backoff=2.0, max_interval=120s |
| Acceptance | Real call against `octocat` returns populated signals. Fake username → `{}`. |

---

## 7. Phase E — Workflow Assembly

### E1. Define `CandidateIndexingWorkflow`

| Field | Value |
|---|---|
| Goal | Orchestrate 7 activities with retry policies + parallel write step |
| Files | `apps/backend/app/temporal/product/candidate_indexing/workflows/candidate_indexing_workflow.py` |
| Input | `CandidateIndexingInput` Pydantic: `{raw_bytes_b64, mime_type, source, source_recruiter_id?}` |
| Output | `IndexingResult`: `{candidate_id, status, completeness_score, was_duplicate}` |
| Sequence | parse_resume → fetch_github_signals → infer_skill_depth → resolve_entity_duplicates → generate_embedding → `asyncio.gather(index_candidate_to_graph, persist_candidate_record)` → score_profile_completeness |
| Decorators | `@WorkflowRegistry.register(WorkflowType.BUSINESS, "converio-queue")` + `@workflow.defn` |
| Query handlers | `@workflow.query def get_status() -> dict` returning `{phase, completeness?, candidate_id?}` |
| Retry policies | Defined as `RetryPolicy` per `workflow.execute_activity` call per Q6 |
| Acceptance | End-to-end Temporal time-skipping test: input bytes → completed workflow with valid `IndexingResult`. |

### E2. Insert `WorkflowRun` row (observability)

| Field | Value |
|---|---|
| Goal | Track workflow execution in PG for SSE/polling |
| Files | `apps/backend/app/temporal/shared/activities/workflow_runs.py` (new shared activity module) |
| Logic | Activities `record_workflow_run_start(workflow_id, workflow_type, candidate_id?)` + `record_workflow_run_complete(workflow_id, status, error?)`. Called from workflow start/end. |
| Acceptance | After workflow completes, `workflow_runs` row exists with `status=completed`, `completed_at` set. |

---

## 8. Phase F — Trigger Surfaces

### F1. API endpoint: `POST /api/v1/candidates/index`

| Field | Value |
|---|---|
| Goal | Recruiter-authenticated upload → fire workflow |
| Files | `apps/backend/app/api/v1/endpoints/candidates.py`, `apps/backend/app/api/v1/router.py`, `apps/backend/app/schemas/product/candidate.py` |
| Request | `multipart/form-data`: `file` (PDF/DOCX), optional `notes` |
| Response | 202 ACCEPTED, `{workflow_id, candidate_indexing_run_id}` |
| Auth | `current_user` → `recruiter_id` (lookup `recruiters` by `supabase_user_id`) |
| Logic | Read bytes, base64-encode, derive `mime_type`, fire `client.start_workflow(CandidateIndexingWorkflow.run, input, id=f"candidate-indexing-{uuid4()}", task_queue="converio-queue")`. Return workflow_id. |
| Acceptance | curl upload PDF → 202 + workflow_id; Temporal UI shows workflow running. |

### F2. Seed dataset + script

| Field | Value |
|---|---|
| Goal | Bootstrap 100 synthetic candidates |
| Files | `apps/backend/tests/fixtures/seed_candidates.json`, `apps/backend/scripts/seed_candidates.py` |
| Fixture | Array of 100 records: `{full_name, email, github_username?, resume_text, seniority, years_experience, location, stage_fit, skills[], work_history[], education[]}` |
| Script | CLI: `python scripts/seed_candidates.py --limit 100`. For each record, render to Markdown text, encode bytes (UTF-8 markdown bytes, mime=`text/markdown` — extend docling parser to accept), fire workflow. Source=`seed`, recruiter null. |
| Acceptance | Script run produces 100 `Candidate` rows + Neo4j subgraphs. Re-run is idempotent (dedup_hash matches). |

---

## 9. Phase G — Tests

### G1. Unit tests per activity

| Field | Value |
|---|---|
| Files | `apps/backend/tests/temporal/test_*_activity.py` |
| Coverage | Each activity in B/C/D — happy path + error path + idempotency check (where relevant) |
| Mocks | `httpx.AsyncClient` for GitHub + LLM HTTP, real PG (via `conftest.py` fixture), real Neo4j (test container) |
| Acceptance | `pytest tests/temporal -k activity` all green; coverage ≥ 80% per activity module. |

### G2. Workflow integration test

| Field | Value |
|---|---|
| Files | `apps/backend/tests/temporal/test_candidate_indexing_workflow.py` |
| Setup | `WorkflowEnvironment.start_time_skipping()`, register all activities + workflow |
| Cases | (1) New candidate end-to-end → `IndexingResult.status="indexed"`. (2) Duplicate candidate → `was_duplicate=True`. (3) Low-completeness profile → `status="review_queue"`. (4) GitHub 404 → workflow completes (graceful). (5) GitHub rate-limit injection → workflow retries + completes. |
| Acceptance | All 5 cases pass. |

### G3. Replay determinism test

| Field | Value |
|---|---|
| Goal | Verify workflow code is replay-safe |
| Logic | Capture event history from G2 case 1, run `Replayer.replay_workflow(history)` — expect no `NondeterminismError`. |
| Acceptance | Replay test passes. |

---

## 10. Phase H — Wire-up & Documentation

### H1. Register workflow in worker

| Field | Value |
|---|---|
| Goal | Confirm `discover_all()` finds new modules |
| Files | none (decorator-driven) |
| Output | `python -m app.temporal.worker` boots, logs registered workflow `CandidateIndexingWorkflow` and 8 activities (7 candidate_indexing + 1 shared workflow_runs). |
| Acceptance | Worker connects, starts polling `converio-queue`. |

### H2. SSE wire-up (optional v1)

| Field | Value |
|---|---|
| Goal | Stream workflow status to FE — defer if FE not yet built |
| Files | `apps/backend/app/api/v1/endpoints/candidates.py` adds `GET /stream/{workflow_id}` |
| Output | `SSEManager` instance polls `WorkflowRun` row + Temporal `query_workflow("get_status")` every 2s |
| Acceptance | Curl SSE shows phase transitions: `parsing → fetching_github → indexing → completed`. |
| Priority | **Defer** to Phase 12 of master plan unless trivial. |

### H3. README + ops doc

| Field | Value |
|---|---|
| Files | `apps/backend/README.md`, optionally `docs/agents/agent_2.md` |
| Output | Section: "Running Agent 2 locally — start docker-compose, set `LLM_PROVIDER=ollama`, `ollama pull qwen2.5:7b`, `python scripts/seed_candidates.py`". |
| Acceptance | Fresh checkout → engineer runs steps → 100 candidates indexed. |

---

## 11. Execution Order Summary

```
A1 → A2 → A3 → A4 → A5 → A6 → A7 → A8 (parallel within Phase A)
A9 (Neo4j refactor — independent, but blocks B5)
A10 → A11

B1, B2, B3, B5 in parallel (deterministic, no deps on C/D)
B4 after B3 (needs persisted row to update status)

C1 after A3 + A7 + A10
C2 after A3 + A10 + B1 (needs profile + signals shape)

D1 after A6 + A10

E1 after all of B + C + D
E2 after E1

F1 after E1
F2 after E1 + F1 (or directly invoke client.start_workflow)

G1 throughout (per-activity as built — TDD friendly)
G2 after E1
G3 after G2

H1 after E1
H2 deferred
H3 last
```

## 12. Effort Estimate

| Phase | Tasks | Effort |
|---|---|---|
| A | 11 | 1.5 days |
| B | 5 | 1 day |
| C | 2 | 0.5 day |
| D | 1 | 0.5 day |
| E | 2 | 0.5 day |
| F | 2 | 0.5 day |
| G | 3 | 1 day |
| H | 3 | 0.5 day |
| **Total** | **29** | **~6 dev-days** |

## 13. Acceptance Criteria — End of Agent 2

1. `python scripts/seed_candidates.py --limit 100` produces 100 rows in `candidates`, full Neo4j subgraph per candidate, ≥95% completeness on dense profiles, status `indexed` or `review_queue`.
2. `POST /api/v1/candidates/index` with PDF returns 202 + `workflow_id`; workflow completes in Temporal UI within ~30s (Ollama) / ~10s (OpenRouter).
3. Re-running seed script is idempotent (no duplicate PG rows or Neo4j nodes).
4. All 5 workflow integration test cases (G2) pass.
5. Worker survives `kill -9` mid-workflow; on restart, Temporal replays cleanly to completion.
6. `LLM_PROVIDER` swap (`ollama` ↔ `openrouter`) requires only env change + worker restart, zero code change.

---

## 14. LLM Strategy Pattern — Reference Snippets

For implementer of A3. Full design discussed in source conversation; condensed shape below.

### 14.1 Base interface

```python
# app/core/llm/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    provider: str
    raw: dict[str, Any] | None = None


class LLMClient(ABC):
    """Strategy interface — every provider implements these methods."""

    provider_name: str

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    @abstractmethod
    async def structured_complete(
        self,
        messages: list[LLMMessage],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> T: ...

    @abstractmethod
    async def close(self) -> None: ...
```

### 14.2 Concrete strategies

- `OpenRouterClient` — httpx async, OpenAI-compatible `/chat/completions`, structured via `response_format={"type":"json_schema",...}`.
- `GeminiClient` — `google-genai` SDK, structured via `response_schema=PydanticModel`.
- `OllamaClient` — httpx async to `http://localhost:11434/api/chat`, structured via `format=<json_schema>` (Ollama ≥ 0.5).

Each constructor takes only its own credentials/host/model. No cross-provider knowledge.

### 14.3 Factory

```python
# app/core/llm/factory.py
from typing import Callable
from app.core.config import settings
from app.core.llm.base import LLMClient
from app.core.llm.gemini_client import GeminiClient
from app.core.llm.ollama_client import OllamaClient
from app.core.llm.openrouter_client import OpenRouterClient

_STRATEGIES: dict[str, Callable[[], LLMClient]] = {
    "openrouter": lambda: OpenRouterClient(
        api_key=settings.llm.openrouter_api_key,
        base_url=settings.llm.openrouter_api_url,
        default_model=settings.llm.openrouter_model,
    ),
    "gemini": lambda: GeminiClient(
        api_key=settings.llm.gemini_api_key,
        default_model=settings.llm.gemini_model,
    ),
    "ollama": lambda: OllamaClient(
        host=settings.llm.ollama_host,
        default_model=settings.llm.ollama_model,
    ),
}

_singleton: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _singleton
    if _singleton is not None:
        return _singleton
    provider = settings.llm.provider.lower()
    if provider not in _STRATEGIES:
        raise ValueError(f"Unknown LLM provider '{provider}'. Valid: {list(_STRATEGIES)}")
    _singleton = _STRATEGIES[provider]()
    return _singleton


async def close_llm_client() -> None:
    global _singleton
    if _singleton is not None:
        await _singleton.close()
        _singleton = None
```

### 14.4 Settings extension

```python
class LLMSettings(BaseSettings):
    provider: str = Field(default="ollama", validation_alias="LLM_PROVIDER")

    # OpenRouter
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_api_url: str = Field(
        default="https://openrouter.ai/api/v1", validation_alias="OPENROUTER_API_URL",
    )
    openrouter_model: str = Field(
        default="google/gemini-2.0-flash-001", validation_alias="OPENROUTER_MODEL",
    )

    # Gemini direct
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.0-flash", validation_alias="GEMINI_MODEL")

    # Ollama (local)
    ollama_host: str = Field(default="http://localhost:11434", validation_alias="OLLAMA_HOST")
    ollama_model: str = Field(default="qwen2.5:7b", validation_alias="OLLAMA_MODEL")
```

### 14.5 `.env.example` additions

```
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b
OPENROUTER_MODEL=google/gemini-2.0-flash-001
```

### 14.6 Recommended local Ollama models

| Model | Size | Use case |
|---|---|---|
| `qwen2.5:7b` | 4.5GB | **Default** — strongest JSON-mode adherence in 7B class |
| `llama3.1:8b` | 5GB | General reasoning fallback |
| `mistral:7b-instruct` | 4GB | Faster, lighter quality |

---

## 15. Docling — Library Choice Rationale

Decision: **docling** over `pypdf` or `pdfplumber`.

| Lib | Layout fidelity | Formats | Output | Weight |
|---|---|---|---|---|
| pypdf | Bad on multi-column | PDF only | Raw text blob | ~1MB pure Python |
| pdfplumber | OK — bbox + tables | PDF only | Text + tables w/ coords | ~10MB |
| **docling** | **Best — ML layout model** | **PDF, DOCX, PPTX, HTML, images** | **Structured Markdown/JSON** | ~hundreds MB; reuses torch already pulled by sentence-transformers |

Reasons:
1. Resumes are multi-column → docling's layout model groups regions correctly.
2. DOCX support — recruiters upload both PDF and Word; one library covers both.
3. Markdown output preserves section headings → cleaner LLM input for skill extraction (no manual section regex).
4. Torch already in image (sentence-transformers dependency) → marginal weight cost.
5. Active project (IBM, Apache 2.0).

Tradeoffs / mitigations:
- Cold start: pre-pull model in `Dockerfile.worker` (Task A8).
- Memory: ~300MB → ~1GB worker RSS. Acceptable for PoW.
- Latency: 2–10s per resume. Set activity `start_to_close_timeout=timedelta(seconds=60)`.
- Determinism: pin docling version; activity result cached in Temporal event history → replay-safe regardless of upstream model drift.

---

## 16. Open Risks / Follow-ups (out of Agent 2 scope)

1. **`hitl_events.actor_id NOT NULL`** — blocks completeness review event insert. v1 workaround: skip event insert. Follow-up migration: make `actor_id` nullable OR introduce a system operator sentinel UUID. Track when building review UI.
2. **Dedup race condition** — two concurrent uploads of same candidate may both pass `resolve_entity_duplicates`. PG `ON CONFLICT (dedup_hash)` and Neo4j MERGE absorb the race; downstream Agent 4 has its own `(job_id, candidate_id, rubric_id)` uniqueness so duplicate scorecards are prevented there.
3. **PDF parsing edge cases** — image-only resumes (no text layer) require docling's OCR pipeline. v1 acceptance: assume text-extractable PDFs. If OCR needed later, enable docling's pipeline option.
4. **Ollama JSON-mode reliability** — weaker than hosted models. Mitigated by Pydantic validation on `structured_complete` + Temporal retry. On persistent validation failures, document fallback to OpenRouter for affected activities.

---

*Plan generated 2026-04-26 from session-driven design with Shreekar. Source conversation captured locked decisions, pre-plan defaults, phased breakdown, and reference snippets. Ready for Phase A execution.*
