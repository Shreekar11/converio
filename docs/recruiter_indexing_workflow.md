# Recruiter Indexing Workflow — Design Doc

> Grounded in the implementation merged across commits `a8bfd6c` → `fbd62bb` (April 2026).
> Reference files: `apps/backend/app/temporal/product/recruiter_indexing/`.

---

## 1. Overview

`RecruiterIndexingWorkflow` builds the **recruiter-side knowledge base** that Agent 0 (Recruiter Assignment Agent — see `docs/contrario_proof_of_work_context.md` §11) needs in order to rank recruiters against an incoming role. Without this workflow, Contrario's "we assign recruiters who have filled your exact role 50+ times" promise has no queryable substrate: the `recruiters` table holds bare wizard rows with no embeddings, no graph nodes, and no derived placement metrics.

The workflow is the recruiter-side mirror of `CandidateIndexingWorkflow`. Both pipelines feed a hybrid retrieval surface (PostgreSQL + pgvector + Neo4j) so downstream agents can search by similarity and traverse by structured relationships in one breath. The recruiter pipeline diverges in two important ways: (a) it is **enrichment-only** — the wizard creates the `Recruiter`, `RecruiterClient`, and `RecruiterPlacement` rows synchronously, the workflow upserts metrics and indexes the graph; (b) it has **zero LLM calls** — domain expertise is captured as enum-locked checkboxes in the wizard, not inferred from free text.

This document is the design-time companion to `docs/plans/recruiter_indexing_plan.md`. Where the plan is task-ordered ("what to build, in what order"), this doc is artifact-ordered ("what got built, how it fits together") — written as the onboarding reference for the next engineer working on either Agent 0 or the recruiter portal.

---

## 2. Architecture Mapping (Recruiter vs Candidate)

| Step | Candidate flow | Recruiter flow | LLM? |
|---|---|---|---|
| 1 | `parse_resume` | **Skip** — wizard provides structured `RecruiterProfile` | — |
| 2 | `fetch_github_signals` | **Skip** — out of scope; future LinkedIn enrichment | — |
| 3 | `infer_skill_depth` | **Skip** — domain expertise comes from wizard checkboxes | — |
| 4 | `resolve_entity_duplicates` | `resolve_recruiter_duplicates` — email lookup (already unique-constrained) | No |
| 5 | (after dedup) | `compute_placement_metrics` — derive `fill_rate_pct`, `avg_days_to_close`, `total_placements` from `RecruiterPlacement` rows | No |
| 6 | `generate_embedding` | `generate_recruiter_embedding` — text blob from bio + domain expertise + workspace_type + recruited_funding_stage + role_focus + placement role_titles | No (local sentence-transformers) |
| 7 | `persist_candidate_record` | `persist_recruiter_record` — **upsert only** (recruiter row already exists); writes metrics + embedding + `extra` snapshot | No |
| 8 | `index_candidate_to_graph` | `index_recruiter_to_graph` — Neo4j MERGE: `Recruiter` + `EXPERTISE_IN→Domain` + `PLACED_AT→CompanyStage` + `FILL_RATE→Metric` | No |
| 9 | `score_profile_completeness` | `score_recruiter_credibility` — wizard-field weighted scoring; `<0.5 → pending`, else `active` | No |

**Skip rationale (one line each):**

- **Step 1** — wizard payload is already structured; defer free-text resume parsing to v2.
- **Step 2** — GitHub is candidate-side signal; recruiter LinkedIn enrichment is out of PoW scope.
- **Step 3** — domain expertise is an enum dropdown in the wizard; nothing to infer.

**Net pipeline: 6 activities, zero LLM calls.** The `_GITHUB_RETRY` policy from the candidate pipeline is dropped; only `_DB_RETRY` and `_EMBED_RETRY` remain.

---

## 3. Trigger Surfaces

Three independent surfaces fire `RecruiterIndexingWorkflow`. All produce a `RecruiterIndexingInput` with `input_kind="profile"`, a `RecruiterProfile`, and a `source` discriminator.

### 3.1 Onboarding API trigger

**Endpoint:** `POST /api/v1/recruiters/{recruiter_id}/index`
**File:** `apps/backend/app/api/v1/endpoints/recruiters.py`

| Field | Value |
|---|---|
| Auth | `get_current_user` (Supabase JWT) — same dependency as candidate endpoint |
| Path param | `recruiter_id: UUID` (must already exist in PG) |
| Request body | none (wizard data already in PG; endpoint loads it) |
| Response | `200 OK` with the full `RecruiterIndexingResult` (`{workflow_id, recruiter_id, status, credibility_score, source}`) |
| Error modes | `404` recruiter not found; `504` workflow exceeded 30s execution timeout (frontend should fall back to a status poll using `workflow_id`); `5xx` other workflow / Temporal failures |
| Source tag | `source="onboarding"` |
| Workflow ID | `recruiter-indexing-{recruiter_id}` |
| Reuse policy | `WorkflowIDReusePolicy.ALLOW_DUPLICATE` |
| Invocation | `client.execute_workflow(...)` (blocking) with `execution_timeout=timedelta(seconds=30)` |

The endpoint reads the canonical `Recruiter` + linked `RecruiterClient` + `RecruiterPlacement` rows out of PG, assembles a `RecruiterProfile`, and **executes** (not just starts) the workflow. Indexing is enrichment-only — no LLM, no external API, total runtime 1-3s — so the wizard gets the full result inline and can redirect to the dashboard with the resulting `status` (active vs pending) without needing SSE/polling. Temporal still owns retry semantics, event history, and the dedicated worker process that hosts the sentence-transformers model. The wizard-write step is intentionally **synchronous** at the HTTP layer (Decision 4) so the workflow only handles enrichment.

`ALLOW_DUPLICATE` is required because the same `recruiter_id` will need to be re-indexed every time the recruiter mutates their portfolio — most commonly via the planned `Add Client` / `Add Placement` modals (still out of scope for this PR, see §9). Without `ALLOW_DUPLICATE`, the second invocation would error out as a workflow-already-exists conflict.

### 3.2 Seed dataset

**Script:** `apps/backend/scripts/seed_recruiters.py`
**Fixture:** `apps/backend/tests/fixtures/seed_recruiters.json`

The fixture holds **25 synthetic recruiters** designed to cover the full enum surface that Agent 0 will traverse:

| Dimension | Distribution |
|---|---|
| `RoleCategory` (domain expertise) | All 5 values: `engineering`, `gtm`, `design`, `ops`, `data` |
| `CompanyStage` (placements) | All 5 values: `pre_seed`, `seed`, `series_a`, `series_b`, `growth` |
| `WorkspaceType` | All 5 values represented |
| Credibility outcome | 16 high-credibility (`active`, ≥0.5) + 9 review-queue (`pending`, <0.5) — 64% / 36% split |

The script inserts `Recruiter` + `RecruiterClient` + `RecruiterPlacement` rows synchronously via repositories (mirroring the wizard write path), then fires the workflow with `source="seed"` and `workflow_id=f"seed-recruiter-{slug(name)}-{i}"`. Re-runs are idempotent: the script catches `WorkflowAlreadyStartedError` from Temporal (same pattern as `seed_candidates.py`).

### 3.3 Workflow ID conventions

| Trigger | Workflow ID format | Example |
|---|---|---|
| Onboarding API | `recruiter-indexing-{recruiter_id}` | `recruiter-indexing-7f3a8c0e-…` |
| Seed script | `seed-recruiter-{slug}-{i}` | `seed-recruiter-amelia-park-3` |

Both share the same workflow class and task queue (`converio-queue`). Only the IDs differ so seed and production runs are easy to distinguish in the Temporal UI.

---

## 4. Activity Contracts

All six activities live under `apps/backend/app/temporal/product/recruiter_indexing/activities/` and are picked up by worker auto-discovery (`@ActivityRegistry.register("recruiter_indexing", "<name>")`). The workflow file applies retry policies at call time — the activities themselves do not declare retries.

### 4.1 `resolve_recruiter_duplicates`

| Field | Value |
|---|---|
| Inputs | `profile: dict` (serialized `RecruiterProfile`) |
| Outputs | `ResolveRecruiterDuplicatesResult` — `{is_duplicate: bool, existing_recruiter_id: str \| None, match_source: "email" \| None}` |
| Dependencies | `RecruiterRepository.get_by_email()` |
| Failure modes | Recruiter row missing → raises `RuntimeError` (workflow fails fast — Decision 4) |
| Idempotency | Read-only, safe for Temporal replay |
| Retry | `_DB_RETRY` (max=3, backoff=1.5×) |

### 4.2 `compute_placement_metrics`

| Field | Value |
|---|---|
| Inputs | `recruiter_id: str` |
| Outputs | `ComputedMetrics` — `{fill_rate_pct: float \| None, avg_days_to_close: float \| None, total_placements: int, placements_by_stage: dict[str, int]}` |
| Dependencies | `RecruiterPlacementRepository.get_by_recruiter()`; reads `recruiters.fill_rate_pct` baseline if pre-set on seed |
| Failure modes | Empty placements → returns zero/null fields (not an error) |
| Idempotency | Read-only |
| Retry | `_DB_RETRY` |

Notes: `Decimal` columns from PG are cast to `float` here (see Decisions §7). `placements_by_stage` is grouped by the `recruiter_placements.company_stage` column added in the migration that ships with `a8bfd6c`.

### 4.3 `generate_recruiter_embedding`

| Field | Value |
|---|---|
| Inputs | `profile: dict` |
| Outputs | `{embedding: list[float]}` — 384-dim |
| Dependencies | `app.core.embeddings.embed_text()` (sentence-transformers `all-MiniLM-L6-v2`, lazy singleton) |
| Failure modes | Model load failure → bubbles up |
| Idempotency | Deterministic for fixed input |
| Retry | `_EMBED_RETRY` (max=2 — fail fast on a deterministic step) |

Text-blob composition (pipe-joined, nulls filtered):

```
full_name | bio | workspace_type | recruited_funding_stage |
domain expertise: <comma-joined> |
past clients: <client_company_name (role_focus csv)> |
past placements: <role_title at company_name (stage)>
```

### 4.4 `persist_recruiter_record`

| Field | Value |
|---|---|
| Inputs | `recruiter_id: str`, `embedding: list[float]`, `metrics: dict` |
| Outputs | `{recruiter_id: str}` |
| Dependencies | `RecruiterRepository`, `async_session_maker()` |
| Failure modes | Recruiter row missing → raises (Decision 4 — wizard guarantees existence) |
| Idempotency | Pure UPDATE; safe to re-run |
| Retry | `_DB_RETRY` |

Updates: `fill_rate_pct`, `avg_days_to_close`, `total_placements`, `embedding`, `extra` (merges `placements_by_stage` snapshot under `extra["placements_by_stage"]`), `updated_at`. Never INSERTs.

### 4.5 `index_recruiter_to_graph`

| Field | Value |
|---|---|
| Inputs | `recruiter_id: str`, `profile: dict`, `metrics: dict` |
| Outputs | `{nodes_merged: int, edges_merged: int}` |
| Dependencies | `Neo4jClientManager.get_session()` |
| Failure modes | Domain value not in `RoleCategory` → raises `ValueError`; Neo4j unreachable → bubbles up |
| Idempotency | All MERGE — re-running leaves graph state unchanged |
| Retry | `_DB_RETRY` |

See §5 for the Cypher schema.

### 4.6 `score_recruiter_credibility`

| Field | Value |
|---|---|
| Inputs | `recruiter_id: str`, `profile: dict` |
| Outputs | `{credibility_score: float, status: "active" \| "pending", review_required: bool}` |
| Dependencies | `RecruiterRepository`, `async_session_maker()` |
| Failure modes | Recruiter row missing → raises |
| Idempotency | Deterministic; final write is `UPDATE recruiters SET status, extra` |
| Retry | `_DB_RETRY` |

See §6 for the weight table and threshold semantics.

---

## 5. Neo4j Graph Schema (Recruiter Side)

The recruiter subgraph is the second half of the §7 master schema. Agent 0 traverses it; indexing only writes it.

```cypher
(:Recruiter {
   id, full_name, status, at_capacity,
   workspace_type, recruited_funding_stage
})
  -[:EXPERTISE_IN {level}]-> (:Domain {name})
  -[:PLACED_AT {role_type, count, avg_days_to_close}]-> (:CompanyStage {stage})
  -[:FILL_RATE {rate_pct, total_roles}]-> (:Metric {kind: "fill_rate"})
```

`Domain.name` is **enum-locked** to `RoleCategory`: `engineering | gtm | design | ops | data`. Unknown values are rejected at activity entry (`index_recruiter_to_graph` raises `ValueError` before writing). Likewise, `CompanyStage.stage` is constrained to the `CompanyStage` enum (`pre_seed | seed | series_a | series_b | growth`).

The `:ASSIGNED_TO -> :Job` edge is **Agent 0 territory** and is not written by indexing.

### 5.1 Example Cypher queries (Agent 0 will use these)

```cypher
-- Find active recruiters with engineering expertise who have placed at series_a
MATCH (r:Recruiter {status: "active", at_capacity: false})
      -[:EXPERTISE_IN]-> (d:Domain {name: "engineering"})
WITH r
MATCH (r) -[:PLACED_AT]-> (cs:CompanyStage {stage: "series_a"})
RETURN r.id, r.full_name
ORDER BY r.full_name
LIMIT 25;
```

```cypher
-- Recruiters with fill rate >= 60%
MATCH (r:Recruiter) -[fr:FILL_RATE]-> (:Metric {kind: "fill_rate"})
WHERE fr.rate_pct >= 0.6
RETURN r.id, r.full_name, fr.rate_pct, fr.total_roles
ORDER BY fr.rate_pct DESC;
```

```cypher
-- Multi-stage placement experience (reasonable proxy for "senior recruiter")
MATCH (r:Recruiter) -[p:PLACED_AT]-> (cs:CompanyStage)
WITH r, count(DISTINCT cs) AS stages_covered, sum(p.count) AS total
WHERE stages_covered >= 2
RETURN r.id, r.full_name, stages_covered, total
ORDER BY total DESC;
```

```cypher
-- Composite query Agent 0 likely runs first: domain + stage + capacity gate
MATCH (r:Recruiter {status: "active", at_capacity: false})
      -[:EXPERTISE_IN]-> (:Domain {name: $role_category})
WITH r
MATCH (r) -[p:PLACED_AT]-> (:CompanyStage {stage: $target_stage})
OPTIONAL MATCH (r) -[fr:FILL_RATE]-> (:Metric {kind: "fill_rate"})
RETURN r.id, r.full_name, p.count AS placements_at_stage,
       coalesce(fr.rate_pct, 0.0) AS fill_rate
ORDER BY fill_rate DESC, placements_at_stage DESC
LIMIT 10;
```

---

## 6. Credibility Scoring

Deterministic weighted sum over wizard-captured signals. No LLM. Weights sum to 1.0.

| Signal | Weight | Condition |
|---|---|---|
| `bio` | 0.15 | non-empty |
| `linkedin_url` | 0.10 | present |
| `domain_expertise` | 0.15 | ≥1 entry |
| `past_clients` | 0.15 | ≥1 entry |
| `past_placements` | 0.20 | ≥3 entries |
| `workspace_type` | 0.10 | set |
| `recruited_funding_stage` | 0.10 | set |
| Multi-stage placements | 0.05 | placements span ≥2 distinct `CompanyStage` |

**Threshold:** `score < 0.5 → status = "pending"` (operator review queue); `score >= 0.5 → status = "active"`.

**Discreteness note:** because every weight steps by 0.05, the score lattice has 5pp granularity. **0.49 is unreachable** — the closest sub-threshold value is **0.45**, the closest at-or-above value is **0.50**. Tests pin both boundaries (see `apps/backend/tests/temporal/activities/test_score_recruiter_credibility.py`) so future weight tweaks that break this property fail fast.

The activity also writes `extra["credibility_score"]` and the breakdown to PG so Agent 0 (and any future operator UI) can read the rationale without recomputing.

---

## 7. Decisions Log

User-confirmed decisions from the plan:

- **D1 — Trust wizard checkboxes for `domain_expertise`.** No LLM inference from bio/placements. Cheaper, deterministic, and good enough given the wizard already structures the data.
- **D2 — Add `company_stage` column to `recruiter_placements`.** Wizard captures via dropdown (cheaper than per-placement LLM stage classification, consistent with the "no LLM unless needed" rule).
- **D3 — `Domain` graph nodes are enum-locked to `RoleCategory`.** Reuses existing `app.schemas.enums.RoleCategory` — no new `Domain` enum needed; activity rejects unknown values at entry.
- **D4 — Wizard writes `Recruiter` + `RecruiterClient` + `RecruiterPlacement` rows synchronously; workflow is enrichment-only.** Workflow upserts onto the existing recruiter row, never inserts. Simplifies failure modes (any "row missing" condition is a real bug).

Implementation-time decisions surfaced during Phase 2:

- **D5 — Cast `Decimal` to `float` in `compute_placement_metrics`.** PG numeric columns deserialize to `Decimal`, but Temporal's JSON-friendly activity boundary requires native floats; Pydantic models on the workflow side declare `float`, so the cast happens at activity exit.
- **D6 — `extra` JSONB merge via dict copy in `persist_recruiter_record`.** SQLAlchemy's unit-of-work does not detect in-place mutation of JSONB columns; copying into a new dict before assignment forces a dirty flag.
- **D7 — Cypher `None` coercion to `""` for non-numeric properties.** Neo4j rejects `null` on stored properties for some types; the activity coerces optional string fields (e.g. `workspace_type`) to empty string before MERGE rather than carrying `None` into the driver.

---

## 8. Verification

### Local end-to-end

```bash
# 1. Apply migration (adds recruiter_placements.company_stage)
cd apps/backend && alembic upgrade head

# 2. Bring up infra + worker
docker compose up postgres neo4j temporal -d
uv run python -m app.temporal.worker

# 3. Seed 25 recruiters
uv run python scripts/seed_recruiters.py --limit 25

# 4. Verify PG
docker exec -it converio-postgres-1 psql -U converio -d converio -c \
  "SELECT full_name, status, fill_rate_pct, embedding IS NOT NULL FROM recruiters LIMIT 5;"

# 5. Verify Neo4j (browser at http://localhost:7474)
#   MATCH (r:Recruiter)-[:EXPERTISE_IN]->(d:Domain) RETURN r.full_name, d.name LIMIT 20;
#   MATCH (d:Domain) WHERE NOT d.name IN ['engineering','gtm','design','ops','data'] RETURN d;
#   -- second query MUST return zero rows
```

### API smoke test

```bash
curl -X POST http://localhost:8000/api/v1/recruiters/{recruiter_id}/index \
  -H "Authorization: Bearer $JWT"
# → 200 OK with {workflow_id, recruiter_id, status, credibility_score, source}.
#   On timeout (rare): 504 with workflow_id — fall back to a Temporal status poll.
```

### Idempotency check

```bash
# Re-running the seed must NOT duplicate Neo4j edges or PG rows
uv run python scripts/seed_recruiters.py --limit 25
uv run python scripts/seed_recruiters.py --limit 25
# In Neo4j: MATCH (:Recruiter)-[e:EXPERTISE_IN]->(:Domain) RETURN count(e);
# Edge count must be identical across runs.
```

### Hermetic tests

```bash
cd apps/backend
uv run pytest tests/temporal/activities/test_resolve_recruiter_duplicates.py \
              tests/temporal/activities/test_compute_placement_metrics.py \
              tests/temporal/activities/test_generate_recruiter_embedding.py \
              tests/temporal/activities/test_persist_recruiter_record.py \
              tests/temporal/activities/test_index_recruiter_to_graph.py \
              tests/temporal/activities/test_score_recruiter_credibility.py -v
uv run pytest tests/temporal/test_recruiter_indexing_workflow.py -v
uv run pytest tests/temporal/test_workflow_replay.py -v
```

Neo4j-dependent tests skip automatically when no Neo4j instance is reachable; the rest of the suite runs hermetically.

---

## 9. Out of Scope (Next Phases)

- **Wizard signup endpoint** — `POST /api/v1/recruiters` for the recruiter sign-up flow. Currently inserted via seed script + assumed pre-existing in the API path.
- **Add Client modal endpoint** — `POST /api/v1/recruiters/{id}/clients`. Would re-fire `RecruiterIndexingWorkflow` to refresh the embedding + graph.
- **Add Placement modal endpoint** — `POST /api/v1/recruiters/{id}/placements`. Same re-index trigger as above.
- **Agent 0 (Recruiter Assignment Agent)** — the consumer of this knowledge base. See §11 of the master context doc.
- **Frontend Recruiter Portal UI** — onboarding wizard, dashboard, role assignment views.
- **Neo4j integration test infra** — the current test suite skips Neo4j-dependent activity tests when no instance is reachable; a docker-compose-backed CI lane is the next step.

---

## 10. Cross-References

**Plan & context docs:**

- `docs/plans/recruiter_indexing_plan.md` — phased task breakdown (the source for this distilled doc)
- `docs/contrario_proof_of_work_context.md` — master architecture (§7 Architecture, §11 Recruiter Assignment Agent)
- `docs/plans/agent_2_candidate_indexing_plan.md` — candidate-side plan (mirror)
- `docs/plans/agent_2_flow.md` — candidate-side implemented flow (mirror)

**Code paths (all paths absolute from repo root):**

- Workflow: `apps/backend/app/temporal/product/recruiter_indexing/workflows/recruiter_indexing_workflow.py`
- Activities: `apps/backend/app/temporal/product/recruiter_indexing/activities/`
  - `resolve_recruiter_duplicates.py`
  - `compute_placement_metrics.py`
  - `generate_recruiter_embedding.py`
  - `persist_recruiter_record.py`
  - `index_recruiter_to_graph.py`
  - `score_recruiter_credibility.py`
- API endpoint: `apps/backend/app/api/v1/endpoints/recruiters.py`
- Seed script: `apps/backend/scripts/seed_recruiters.py`
- Seed fixture: `apps/backend/tests/fixtures/seed_recruiters.json`
- Pydantic schemas: `apps/backend/app/schemas/product/recruiter.py`
- Tests:
  - `apps/backend/tests/temporal/activities/test_*recruiter*.py` (6 files)
  - `apps/backend/tests/temporal/test_recruiter_indexing_workflow.py`
  - `apps/backend/tests/temporal/test_workflow_replay.py` (extended for this workflow)

---

*Distilled from `docs/plans/recruiter_indexing_plan.md` and the implemented code under `apps/backend/app/temporal/product/recruiter_indexing/`. Update this doc when activity contracts, graph schema, or scoring weights change — Agent 0 design depends on it.*
