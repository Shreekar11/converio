# Agent 1 — Job Intake Workflow: Implementation Plan

## Context

`feat/indexing-workflows` is merged to `main`. Candidate + recruiter indexing workflows ship; the recruiter pool is now queryable via Neo4j + pgvector. Per the priority order in `docs/contrario_proof_of_work_context.md` Appendix step 5, the next workflow to build is **Agent 1 — Job Intake**.

Job Intake is the **root** Temporal workflow. Every downstream agent (0, 3, 4, 5, 6) hangs off it. Without the `Job` row + versioned `Rubric`, no agent has anything to score against. The design doc `docs/job_intake_workflow.md` covers the architecture in full.

This PR is **scoped to Job Intake only**. Agents 0/3/4/5/6 ship in their own PRs. Where the parent workflow would normally spawn child workflows, this PR ends after classification + rubric persistence with `jobs.status="recruiter_assignment"` — the child-workflow call sites are stubbed for the next PR to fill in.

The blocking prerequisite is **company onboarding**: `companies` + `company_users` tables exist in PG but have no API write path. A `Job` cannot be created without a `company_id`, so this PR also ships the operator-only company / company-user endpoints.

---

## Locked Decisions (from design doc §7)

| # | Decision |
|---|---|
| D1 | Job Intake API is **fire-and-forget** (`202 Accepted` + `start_workflow`), unlike recruiter indexing (`200` + `execute_workflow`). Full pipeline is multi-day. |
| D2 | Classification fields live on `Job`; rubric on `Rubric`. Rubric is versioned; classification is one-shot. |
| D3 | `WorkflowIDReusePolicy.REJECT_DUPLICATE` — re-intake is a bug; reeval flows through HITL #2 (separate PR). |
| D4 | Agent 0 = child workflow, **stubbed** in this PR (returns immediately with empty assignment list). |
| D5 | Sourcing Agent (Agent 3) is fallback-only — not spawned by intake; out of this PR. |
| D6 | Rubric weights normalize, not validate-and-fail. Drift > 0.05 is logged. |
| D7 | Operator-only company creation. No self-serve company signup in PoW. |
| D8 | `intake_notes` feeds **both** LLM activities (classify + rubric). One field, two prompts. |
| D9 | Workflow exits after `persist_job_record` in this PR. `jobs.status="recruiter_assignment"` is the terminal state until Agent 0 PR. |

---

## Pre-Plan Defaults

| # | Question | Default |
|---|---|---|
| Q1 | LLM provider for classify/rubric? | OpenRouter `gemini-2.0-flash` (matches indexing pipeline + master doc §18). Reuse existing `app.core.llm` client. |
| Q2 | `WorkflowRun` row write — workflow or activity? | Activity (`persist_job_record` writes the row). Workflow stays deterministic. |
| Q3 | Company onboarding auth — operator or admin? | Operator-only via new `get_current_operator` dependency. |
| Q4 | Rate limit on `/jobs/intake`? | 10/hour per `company_id`. Reuse existing rate-limit middleware if present; otherwise scaffold a SlowAPI-style dep. |
| Q5 | `JobIntakeRequest` validation library? | Pydantic v2 via OpenAPI codegen. Spec is source of truth. |
| Q6 | `RoleClassification` & `EvaluationRubric` — generated or hand-rolled? | Generated `app/schemas/generated/jobs.py` for HTTP boundary; hand-rolled `app/schemas/product/job.py` for workflow IO (matches recruiter pattern). |
| Q7 | Seed companies count? | 8 companies across `CompanyStage` distribution; 3 operators. Mirror `seed_recruiters.py` shape. |
| Q8 | LLM retry policy values? | `_LLM_RETRY` = max=3, backoff=2× exponential. Reuse from existing activities if available; otherwise define in workflow. |

---

## Phase A — Infrastructure & Prerequisites

### A1. OpenAPI specs

| Field | Value |
|---|---|
| Goal | Spec is source of truth (per `docs/conventions/openapi-workflow.md`). Ship before any endpoint code. |
| Files | `apps/backend/app/api/v1/specs/companies.json` (new), `apps/backend/app/api/v1/specs/jobs.json` (new) |
| `companies.json` covers | `POST /companies`, `POST /companies/{id}/users`, `GET /companies`, `GET /companies/{id}`, `GET /companies/{id}/users` |
| `jobs.json` covers | `POST /jobs/intake` only (downstream endpoints add later) |
| Acceptance | Both files validate as OpenAPI 3.x; enum types reference `CompanyStage`, `CompanySizeRange`, `RemoteOnsite`, `JobStatus` from existing `app/schemas/enums.py`. |

### A2. Codegen wiring

| Field | Value |
|---|---|
| Goal | Add `companies.json` + `jobs.json` to backend Makefile + frontend `package.json` codegen scripts. |
| Files | `apps/backend/Makefile` (extend `generate-schemas`), `apps/frontend/package.json` (extend `generate:schemas` script) |
| Output | `apps/backend/app/schemas/generated/companies.py`, `apps/backend/app/schemas/generated/jobs.py` |
| Acceptance | `make generate-schemas` succeeds; both generated files import without errors. |

### A3. Operator auth dependency

| Field | Value |
|---|---|
| Goal | New FastAPI dependency that resolves the JWT-authenticated user to an `Operator` row. |
| Files | `apps/backend/app/core/auth.py` (extend) |
| Logic | After `get_current_user` resolves the Supabase JWT, query `operators` by `supabase_user_id`. Return the row if `status="active"`; else raise `403`. |
| Output | `get_current_operator(...) -> Operator` exported from `app.core.auth` |
| Tests | `tests/api/test_auth.py` — mocked JWT yielding active/inactive/missing operator → 200/403/403 |
| Acceptance | Reusable across the new company endpoints. |

### A4. Product schemas — `Job`

| Field | Value |
|---|---|
| Goal | Hand-rolled Pydantic models for workflow IO (separate from generated HTTP schemas). |
| Files | `apps/backend/app/schemas/product/job.py` (new), `apps/backend/app/schemas/product/__init__.py` (extend exports) |
| Models | `JobIntakeInput`, `RoleClassification`, `RubricDimension`, `EvaluationRubric`, `JobIntakeResult` |
| Acceptance | Importable by workflow + activities; field types use `RoleCategory`, `Seniority`, `RemoteOnsite`, `CompanyStage` enums. |

### A5. Seed scripts

| Field | Value |
|---|---|
| Goal | Synthetic operators + companies for end-to-end dev runs. |
| Files | `apps/backend/scripts/seed_operators.py`, `apps/backend/scripts/seed_companies.py`, fixtures `apps/backend/tests/fixtures/seed_operators.json`, `apps/backend/tests/fixtures/seed_companies.json` |
| Distribution | 3 operators (active); 8 companies covering all 5 `CompanyStage` values. |
| Idempotency | Skip on conflict via existing email/name unique constraints (mirror `seed_recruiters.py`). |
| Acceptance | Re-running the script does not duplicate rows. |

---

## Phase B — Company Onboarding API

### B1. `POST /companies` — create company

| Field | Value |
|---|---|
| Goal | Operator creates a client company. No workflow. |
| File | `apps/backend/app/api/v1/endpoints/companies.py` (new); register on `apps/backend/app/api/v1/router.py` |
| Auth | `get_current_operator` |
| Body | Generated `CompanyCreate` (from spec) |
| Validation | `name` 1–200; `stage ∈ CompanyStage`; `company_size_range ∈ CompanySizeRange`; `website` blocks internal/metadata IPs (per CLAUDE.md outbound HTTP rule) |
| Errors | `409` duplicate name (case-insensitive); `403` non-operator; `422` validation |
| Acceptance | Smoke test inserts row; duplicate name returns 409. |

### B2. `POST /companies/{id}/users` — provision hiring-manager seat

| Field | Value |
|---|---|
| Goal | Operator provisions a hiring-manager seat. Email-only invite (Supabase auth fills `supabase_user_id` on first login). |
| File | same as B1 |
| Auth | `get_current_operator` |
| Body | `CompanyUserCreate` — `{email, full_name?, role: hiring_manager \| admin}` |
| Errors | `404` company; `409` email already seated; `403` non-operator |
| Acceptance | Insert; duplicate email returns 409. |

### B3. Read endpoints

| Field | Value |
|---|---|
| Goal | List + detail for operator console + future hiring-manager portal lookup. |
| Endpoints | `GET /companies`, `GET /companies/{id}`, `GET /companies/{id}/users` |
| Auth | `get_current_operator` (all three) |
| Pagination | Cursor-based or simple `limit/offset` (match existing endpoints style — check `recruiters.py`) |
| Acceptance | Returns seeded companies; 404 on missing id. |

### B4. Repository extensions (if needed)

| Field | Value |
|---|---|
| Goal | Surface methods consumed by B1–B3. |
| Files | `apps/backend/app/repositories/companies.py`, `apps/backend/app/repositories/company_users.py` (new — none exists yet for `CompanyUser`) |
| Methods | `CompanyRepository.get_by_name`, `.list_paginated`; `CompanyUserRepository.get_by_email`, `.list_for_company` |
| Acceptance | Used by endpoints; covered by B5 tests. |

### B5. Endpoint tests

| Field | Value |
|---|---|
| Goal | Cover happy + error paths for B1–B3. |
| Files | `apps/backend/tests/api/test_companies_endpoints.py`, `apps/backend/tests/api/test_company_users_endpoints.py` |
| Coverage | Auth (operator vs non-operator), validation, duplicate handling, list/detail, 404 |

---

## Phase C — Job Intake Activities

### C1. `classify_role_type`

| Field | Value |
|---|---|
| Goal | LLM extracts `RoleClassification` from `(title, jd_text, intake_notes)`. |
| File | `apps/backend/app/temporal/product/job_intake/activities/classify_role_type.py` (new) |
| Registry | `@ActivityRegistry.register("job_intake", "classify_role_type")` |
| Inputs | `{title: str, jd_text: str, intake_notes: str \| None}` |
| Outputs | `RoleClassification` (Pydantic) — see §A4 |
| Logic | Build prompt with system instructions + delimited user content (per CLAUDE.md AI/LLM rules — no raw user input in privileged prompts). Validate output against Pydantic; sort + dedupe `must_have_skills` / `nice_to_have_skills` for replay-safety. |
| Failure modes | Invalid enum → raise `ValueError`; LLM 5xx → bubble up for `_LLM_RETRY` |
| Retry | `_LLM_RETRY` (max=3, backoff=2×) — declared at workflow call site, not activity |
| Tests | `tests/temporal/activities/test_classify_role_type.py` — mocked LLM client; assert enum coercion, sort, dedup, replay determinism |

### C2. `generate_evaluation_rubric`

| Field | Value |
|---|---|
| Goal | LLM produces a 4–8 dimension weighted rubric. |
| File | `apps/backend/app/temporal/product/job_intake/activities/generate_evaluation_rubric.py` (new) |
| Registry | `@ActivityRegistry.register("job_intake", "generate_evaluation_rubric")` |
| Inputs | `{classification: RoleClassification, intake_notes: str \| None, extra: dict \| None}` |
| Outputs | `EvaluationRubric` |
| Logic | Prompt LLM for dimensions; renormalize weights to sum=1.0 (warn-log if drift > 0.05 — D6); enforce 4 ≤ len(dimensions) ≤ 8 (truncate top by weight); deterministic sort `(-weight, name)`. |
| Failure modes | Empty dimensions → raise; LLM 5xx → bubble up |
| Retry | `_LLM_RETRY` |
| Tests | Mocked LLM; assert normalization, dimension cap, sort, replay determinism |

### C3. `persist_job_record`

| Field | Value |
|---|---|
| Goal | Write classification onto `Job`, INSERT `Rubric` v1, transition `Job.status` to `recruiter_assignment`, write `WorkflowRun` row. |
| File | `apps/backend/app/temporal/product/job_intake/activities/persist_job_record.py` (new) |
| Registry | `@ActivityRegistry.register("job_intake", "persist_job_record")` |
| Inputs | `{job_id: str, classification: dict, rubric: dict, workflow_id: str}` |
| Outputs | `{job_id: str, rubric_id: str, rubric_version: 1}` |
| Logic | Single transaction: UPDATE `Job` (fill classification fields if null; set `status="recruiter_assignment"`); INSERT `Rubric` with `version=1`; UPSERT `WorkflowRun` row (status="completed"). Wrap rubric INSERT in `try/except IntegrityError` to swallow duplicate on Temporal replay. |
| Failure modes | Job missing → raise (intake API guarantees existence) |
| Retry | `_DB_RETRY` (max=3, backoff=1.5×) |
| Tests | `test_persist_job_record.py` — happy path, replay-safe duplicate INSERT, missing-job raises, status transition |

---

## Phase D — Workflow Assembly

### D1. `JobIntakeWorkflow`

| Field | Value |
|---|---|
| Goal | Root workflow that orchestrates classify → rubric → persist. Stubs the downstream Agent 0 call. |
| File | `apps/backend/app/temporal/product/job_intake/workflows/job_intake_workflow.py` (new) |
| Registry | `@workflow.defn(name="JobIntakeWorkflow")`; auto-discovered via `WorkflowRegistry` |
| Inputs | `JobIntakeInput` (model_validate raw dict) |
| Outputs | `JobIntakeResult` — `{job_id, rubric_id, rubric_version, status: "recruiter_assignment"}` |
| Sequencing | (1) `classify_role_type` (60s timeout, `_LLM_RETRY`), (2) `generate_evaluation_rubric` (60s, `_LLM_RETRY`), (3) `persist_job_record` (15s, `_DB_RETRY`) |
| Stub for Agent 0 | Comment block + TODO marking the future `execute_child_workflow("RecruiterAssignmentWorkflow", ...)` call site. **No actual call** in this PR. |
| Query handlers | `status() -> str` reading `jobs.status` mirror; `current_phase() -> str` for SSE later |
| Acceptance | Replays cleanly under `tests/temporal/test_workflow_replay.py`; deterministic across re-runs. |

### D2. Worker registration

| Field | Value |
|---|---|
| Goal | Worker picks up the new workflow + activities. |
| Files | `apps/backend/app/temporal/worker.py` (verify auto-discovery), `apps/backend/app/temporal/core/discovery.py` (verify) |
| Acceptance | `python -m app.temporal.worker` logs `JobIntakeWorkflow` + 3 activities registered. |

### D3. Workflow integration test

| Field | Value |
|---|---|
| Goal | End-to-end workflow with mocked LLM activities and a real PG fixture. |
| File | `apps/backend/tests/temporal/test_job_intake_workflow.py` (new) |
| Coverage | Happy path; classify error → retry → succeed; persist failure → workflow fails; verify `jobs.status` transition + `Rubric` v1 insertion. |

### D4. Replay determinism test

| Field | Value |
|---|---|
| Goal | Extend existing replay suite. |
| File | `apps/backend/tests/temporal/test_workflow_replay.py` (extend) |
| Coverage | Save event history from a happy-path run; replay → no nondeterminism error. |

---

## Phase E — Trigger Surfaces

### E1. `POST /api/v1/jobs/intake`

| Field | Value |
|---|---|
| Goal | Hiring-manager (or operator-on-behalf-of) submits intake; row created; workflow fired fire-and-forget. |
| File | `apps/backend/app/api/v1/endpoints/jobs.py` (new); register on router |
| Auth | `get_current_user` resolved to `CompanyUser` (mirror recruiter resolution in `candidates.py`); fallback to `get_current_operator` |
| Body | Generated `JobIntakeRequest` |
| Steps | (1) Validate body; (2) verify user is seated at `company_id` (or is operator); (3) INSERT `Job` row with `status="intake"`, `workflow_id=f"job-intake-{job_id}"`; (4) `start_workflow("JobIntakeWorkflow", ..., id_reuse_policy=REJECT_DUPLICATE, task_queue="converio-queue")`; (5) return `202 {job_id, workflow_id, status: "intake"}` |
| Rate limit | 10/hour/`company_id` |
| Errors | `404` company missing; `403` user not seated; `422` validation; `429` rate limit |
| Tests | `tests/api/test_jobs_endpoints.py` — happy path, 403 unseated user, 404 missing company, 422 invalid body, 429 burst |

### E2. Seed jobs script (optional, can defer)

| Field | Value |
|---|---|
| Goal | 5–10 synthetic intakes for end-to-end demos. Workflow ID `seed-job-{slug}-{i}`. |
| File | `apps/backend/scripts/seed_jobs.py` (new, optional in this PR) |
| Acceptance | Re-runs idempotent; firing under `WorkflowAlreadyStartedError` swallowed (mirror `seed_candidates.py`). |
| Defer? | OK to defer to next PR if scope tight. |

---

## Phase F — Documentation & Wire-up

### F1. Update memory + status

| Field | Value |
|---|---|
| Goal | Mark Agent 1 done in implementation status memory. |
| File | `~/.claude/projects/-Users-omkar-gade-Desktop-Personel-shreekar-converio/memory/project_implementation_status.md` |

### F2. Update design doc post-build

| Field | Value |
|---|---|
| Goal | Convert forward-looking sections of `docs/job_intake_workflow.md` into implemented behavior (mirror what was done with `recruiter_indexing_workflow.md`). |

### F3. README / dev runbook (light)

| Field | Value |
|---|---|
| Goal | One section in `apps/backend/README.md` covering: seed companies → submit intake → check Temporal UI. |

---

## Files to Create / Modify (Summary)

**New files:**
```
apps/backend/app/api/v1/specs/companies.json
apps/backend/app/api/v1/specs/jobs.json
apps/backend/app/api/v1/endpoints/companies.py
apps/backend/app/api/v1/endpoints/jobs.py
apps/backend/app/schemas/product/job.py
apps/backend/app/repositories/company_users.py
apps/backend/app/temporal/product/job_intake/__init__.py
apps/backend/app/temporal/product/job_intake/activities/__init__.py
apps/backend/app/temporal/product/job_intake/activities/classify_role_type.py
apps/backend/app/temporal/product/job_intake/activities/generate_evaluation_rubric.py
apps/backend/app/temporal/product/job_intake/activities/persist_job_record.py
apps/backend/app/temporal/product/job_intake/workflows/__init__.py
apps/backend/app/temporal/product/job_intake/workflows/job_intake_workflow.py
apps/backend/scripts/seed_operators.py
apps/backend/scripts/seed_companies.py
apps/backend/tests/fixtures/seed_operators.json
apps/backend/tests/fixtures/seed_companies.json
apps/backend/tests/api/test_companies_endpoints.py
apps/backend/tests/api/test_company_users_endpoints.py
apps/backend/tests/api/test_jobs_endpoints.py
apps/backend/tests/api/test_auth.py
apps/backend/tests/temporal/activities/test_classify_role_type.py
apps/backend/tests/temporal/activities/test_generate_evaluation_rubric.py
apps/backend/tests/temporal/activities/test_persist_job_record.py
apps/backend/tests/temporal/test_job_intake_workflow.py
docs/plans/job_intake_plan.md
```

**Modified files:**
```
apps/backend/app/core/auth.py                   (add get_current_operator)
apps/backend/app/api/v1/router.py               (register companies + jobs routers)
apps/backend/app/repositories/companies.py      (add helper methods)
apps/backend/app/schemas/product/__init__.py    (export job models)
apps/backend/app/schemas/generated/__init__.py  (export generated companies/jobs)
apps/backend/Makefile                           (extend generate-schemas)
apps/frontend/package.json                      (extend generate:schemas)
apps/backend/tests/temporal/test_workflow_replay.py  (extend for JobIntakeWorkflow)
docs/job_intake_workflow.md                     (post-build edit pass)
```

**Reused (no edit):**
```
apps/backend/app/database/models.py           (Job, Rubric, Company, CompanyUser, Operator)
apps/backend/app/schemas/enums.py             (RoleCategory, Seniority, CompanyStage, ...)
apps/backend/app/repositories/jobs.py         (JobRepository)
apps/backend/app/repositories/rubrics.py      (RubricRepository)
apps/backend/app/repositories/operators.py    (OperatorRepository)
apps/backend/app/temporal/core/activity_registry.py
apps/backend/app/temporal/core/workflow_registry.py
apps/backend/app/core/auth.py                 (existing get_current_user)
apps/backend/app/core/llm.py                  (existing LLM client; reuse for both LLM activities)
apps/backend/app/core/database.py             (async_session_maker)
```

---

## Implementation Sequence (PR-friendly order)

1. **A1, A2** — specs + codegen wiring (smallest, validates the schema-first contract)
2. **A3** — operator auth dep + tests
3. **A4** — product schemas (Pydantic models for workflow IO)
4. **A5** — seed operators + companies
5. **B4 → B1, B2, B3 → B5** — repositories → endpoints → tests
6. **C1, C2, C3 (+ tests each)** — activities, in order, each landing with its unit test
7. **D1, D2, D3, D4** — workflow assembly + integration + replay tests
8. **E1** — `/jobs/intake` endpoint + tests
9. **E2** — seed jobs (optional)
10. **F1, F2, F3** — docs + memory update

Each phase is a green CI lane before moving on.

---

## Verification

### Local end-to-end (post-build)

```bash
cd apps/backend && alembic upgrade head
docker compose up postgres neo4j temporal -d
uv run python -m app.temporal.worker   # in another terminal

# Seed prereqs
uv run python scripts/seed_operators.py
uv run python scripts/seed_companies.py
uv run python scripts/seed_recruiters.py --limit 25   # for downstream PRs

# Operator creates company + hiring-manager seat
curl -X POST localhost:8000/api/v1/companies \
  -H "Authorization: Bearer $OPERATOR_JWT" \
  -d '{"name":"Stripe (test)","stage":"growth"}'
# → 201 {id, name, status, created_at}

curl -X POST localhost:8000/api/v1/companies/$COMPANY_ID/users \
  -H "Authorization: Bearer $OPERATOR_JWT" \
  -d '{"email":"hm@stripe.test","role":"hiring_manager"}'
# → 201

# Hiring manager submits intake
curl -X POST localhost:8000/api/v1/jobs/intake \
  -H "Authorization: Bearer $HM_JWT" \
  -d '{"company_id":"...","title":"Founding Engineer",
       "jd_text":"...","intake_notes":"small team, generalist preferred"}'
# → 202 {job_id, workflow_id, status: "intake"}

# Verify in PG
docker exec converio-postgres-1 psql -U converio -d converio -c \
  "SELECT id, title, role_category, status, workflow_id FROM jobs ORDER BY created_at DESC LIMIT 5;"
docker exec converio-postgres-1 psql -U converio -d converio -c \
  "SELECT job_id, version, jsonb_array_length(dimensions) AS dim_count FROM rubrics;"

# Verify in Temporal UI (http://localhost:8080)
#   JobIntakeWorkflow with id=job-intake-{job_id} → status: completed
```

### Hermetic tests

```bash
cd apps/backend
uv run pytest tests/api/test_companies_endpoints.py \
              tests/api/test_company_users_endpoints.py \
              tests/api/test_jobs_endpoints.py \
              tests/api/test_auth.py -v
uv run pytest tests/temporal/activities/test_classify_role_type.py \
              tests/temporal/activities/test_generate_evaluation_rubric.py \
              tests/temporal/activities/test_persist_job_record.py -v
uv run pytest tests/temporal/test_job_intake_workflow.py -v
uv run pytest tests/temporal/test_workflow_replay.py -v
```

### Idempotency

```bash
# Re-running seed scripts must not duplicate operators/companies
uv run python scripts/seed_operators.py
uv run python scripts/seed_companies.py
docker exec converio-postgres-1 psql -U converio -d converio -c \
  "SELECT count(*) FROM operators; SELECT count(*) FROM companies;"
# → identical counts before and after second run
```

### Codegen drift check

```bash
cd apps/backend && make generate-schemas
git diff --exit-code app/schemas/generated/companies.py app/schemas/generated/jobs.py
# → no diff (CI lane will fail if spec changes without regenerated code)
```

---

## Acceptance Criteria

- [ ] `companies.json` + `jobs.json` OpenAPI specs ship; backend + frontend codegen wired.
- [ ] `get_current_operator` dependency works with seeded operators.
- [ ] All 5 company endpoints (B1–B3) return correct status codes for happy + error paths.
- [ ] `seed_operators.py` + `seed_companies.py` run idempotently.
- [ ] 3 activities each have unit tests with mocked dependencies; tests pass hermetically (no PG/Neo4j needed for activity tests).
- [ ] `JobIntakeWorkflow` runs end-to-end in `tests/temporal/test_job_intake_workflow.py`.
- [ ] Workflow replay test passes (no nondeterminism flagged).
- [ ] `POST /jobs/intake` returns 202 and the workflow completes with `jobs.status="recruiter_assignment"` + `Rubric` v1 row in PG.
- [ ] Rate limit on `/jobs/intake` enforced (429 on burst).
- [ ] No security warnings — auth on every endpoint, parameterized queries, no secrets, error responses generic, JD/intake content not echoed back in 5xx bodies.
- [ ] `docs/job_intake_workflow.md` updated post-build to reflect implemented state.

---

## Out of Scope (for this PR)

- `RecruiterAssignmentWorkflow` (Agent 0) — the parent has a stub call site only.
- HITL #1 (`operator_approval` Signal) — Agent 0 owns it.
- HITL #2 (`company_review` Signal) — Agent 5 / Ranking PR.
- `ScorecardGeneratorWorkflow`, `RankingAgentWorkflow`, `SourcingAgentWorkflow`, `AmbientMonitorWorkflow`.
- Frontend portals (Company, Recruiter, Operator).
- SSE streaming for workflow status.
- ATS push, Slack notifications.
- `seed_jobs.py` (optional in this PR; defer if scope tight).

---

## Conventions (per `.claude/rules/`)

- **Python:** PEP 8 + type hints + structured logging via `app.utils.logging.get_logger` (see `python-guidelines.mdc`).
- **FastAPI:** `APIRouter` per domain, `async def` everywhere, unique `operation_id` per route, `response_model` mandatory, `HTTPException` for errors (see `fastapi.mdc`).
- **TDD:** Tests first; mock side effects (DB, LLM, external APIs); test behavior not implementation; mark internal helpers with `_` prefix and only test through public interfaces (see `test-driven-development.mdc`).
- **Commits:** `<type>: <imperative>` lowercase; no `Phase N` tags; preserve case for proper nouns (`LLM`, `API`, `Neo4j`, `FastAPI`, `Temporal`, `OpenAPI`); one logical change per commit (see `commit-message.mdc`).

---

## Sub-Agent Dispatch Protocol

Each phase is dispatched to a `backend-architect` sub-agent in sequence. Each sub-agent:

1. Reads `docs/plans/job_intake_plan.md` first for full context.
2. Reads `docs/job_intake_workflow.md` for design decisions.
3. Reads relevant `.claude/rules/*.mdc` files (Python, FastAPI, TDD, commit conventions).
4. Implements only its assigned phase — does not touch other phases.
5. Runs tests for its phase before reporting done.
6. Reports the diff summary back to the parent.

After each phase the parent commits the work via `git commit` following `commit-message.mdc`.

---

*Sources: `docs/job_intake_workflow.md` (design doc, this PR's authoritative spec); `docs/contrario_proof_of_work_context.md` §10, §15, §19; sibling plans `docs/plans/recruiter_indexing_plan.md` + `docs/plans/agent_2_candidate_indexing_plan.md` (style + structure).*
