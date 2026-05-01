# Job Intake Workflow — Design Doc

> Companion to `docs/contrario_proof_of_work_context.md` §10 (Phase 2 — Job Intake) and §7 (Architecture).
> Mirrors the format of `docs/recruiter_indexing_workflow.md`.
> Status: implemented in branch `feat/job-intake-workflow` (2026-05-01). Reference files: `apps/backend/app/temporal/product/job_intake/`. See §10 Out of Scope for what is intentionally deferred to follow-up PRs.

---

## 1. Overview

`JobIntakeWorkflow` is the **root Temporal workflow** for every role Converio takes on. A company-side managed-intake submission (role details, JD, intake notes, must-haves, red flags, stage, comp range) fires this workflow; everything downstream — recruiter assignment, candidate sourcing, scorecard generation, ranking, ambient monitoring, both human-in-the-loop pauses — runs as child workflows or signals nested under it.

It is structurally the mirror image of `RecruiterIndexingWorkflow` and `CandidateIndexingWorkflow`:

| Indexing workflows | Job Intake workflow |
|---|---|
| Per-recruiter / per-candidate | Per-role (`Job`) |
| Enrichment of an existing PG row | Creates the `Job` + `Rubric` rows from raw intake payload |
| 6–8 activities, 0–2 LLM calls | 3 activities, 2 LLM calls |
| No child workflows | Spawns 4 child workflow types over its lifetime |
| No human-in-the-loop | Two HITL Signal pauses (operator approval + company review) |
| Runtime 1–10s | Runtime hours → days (HITL gates dominate) |

The differentiator at this layer is **classification + rubric generation**. Every downstream agent (Agent 0 fit scoring, Agent 4 scorecard, Agent 5 ranking) consumes the rubric. A bad rubric poisons the entire pipeline, so generating it deterministically and persisting it versioned is the central job of the intake workflow.

This doc is the design-time companion to a future `docs/plans/job_intake_plan.md`. It captures everything the workflow needs from upstream (prerequisites — §2), the workflow itself (§3–§7), and how it orchestrates Agents 0/3/4/5/6 (§8).

---

## 2. Prerequisites — What Must Ship Before Execution

`JobIntakeWorkflow` cannot fire until the data and surfaces below exist. The schema is already migrated; the API surface is the gap.

### 2.1 Data prerequisites (already done)

| Requirement | Status | Location |
|---|---|---|
| `companies` table | ✅ migrated | `alembic/versions/0001_initial_schema.py` |
| `company_users` table | ✅ migrated | same |
| `jobs` table (with `workflow_id`, `status`, `extra`) | ✅ migrated | same |
| `rubrics` table (versioned, `(job_id, version)` unique) | ✅ migrated | same |
| `JobStatus` enum (`intake → recruiter_assignment → sourcing → scoring → review → closed`) | ✅ | `app/schemas/enums.py` |
| `RoleCategory`, `Seniority`, `RemoteOnsite`, `CompanyStage` enums | ✅ | same |
| `JobRepository.get_with_rubric` / `get_by_workflow_id` | ✅ | `app/repositories/jobs.py` |
| `RubricRepository.get_latest_for_job` | ✅ | `app/repositories/rubrics.py` |
| Recruiter pool indexed (Agent 0 substrate) | ✅ post-merge of `feat/indexing-workflows` | `app/temporal/product/recruiter_indexing/` |

### 2.2 Onboarding API gap — **must build first**

Before any company can submit a managed intake, Converio operators (or, post-PoW, an onboarding-call form) must be able to **register a company** and **provision hiring-manager seats**. Today there is no endpoint for either — the `companies` and `company_users` tables exist but have no write path.

Two new endpoints are required, both gated by Operator-only auth (Converio talent-ops creates the company; the hiring manager comes in later via Supabase invite):

#### 2.2.1 `POST /api/v1/companies` — Create company (operator-only)

| Field | Value |
|---|---|
| Auth | `get_current_operator` (new dependency — checks `operators.supabase_user_id` matches JWT) |
| Body | `CompanyCreate` — `{name, stage?, industry?, website?, company_size_range?, founding_year?, hq_location?, description?}` |
| Validation | `stage ∈ CompanyStage`; `company_size_range ∈ CompanySizeRange`; `website` URL allowlist (no `localhost`/internal IPs — see CLAUDE.md outbound HTTP rule); `name` 1–200 chars |
| Response | `201 Created` with `{id, name, status, created_at}` |
| Error modes | `409` duplicate name (case-insensitive); `403` non-operator; `422` validation |
| Side effects | INSERT `companies` row with `status="active"`. No workflow fires. |

#### 2.2.2 `POST /api/v1/companies/{company_id}/users` — Provision hiring-manager seat

| Field | Value |
|---|---|
| Auth | `get_current_operator` |
| Body | `CompanyUserCreate` — `{email, full_name?, role: hiring_manager \| admin}` |
| Validation | Email format; `role ∈ CompanyUserRole` |
| Response | `201 Created` with `{id, company_id, email, role}` |
| Error modes | `404` company; `409` email already seated; `403` non-operator |
| Side effects | INSERT `company_users` row with `supabase_user_id=null` (filled in on first Supabase login via JWT-side hook — same pattern as `recruiters.supabase_user_id`). |

#### 2.2.3 Read endpoints (small, ship same PR)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/companies` | Operator-only list with paginated company status |
| `GET /api/v1/companies/{id}` | Operator + linked `company_users` (hiring manager portal lookup) |
| `GET /api/v1/companies/{id}/users` | Operator-only seat list |

#### 2.2.4 OpenAPI + codegen prerequisites

Per `docs/conventions/openapi-workflow.md` the spec ships first:

- Add `apps/backend/app/api/v1/specs/companies.json` covering both write + read endpoints.
- Extend `Makefile` `generate-schemas` target with a `companies.json` line.
- Extend frontend `package.json` with `pnpm generate:companies`.
- Run codegen → `apps/backend/app/schemas/generated/companies.py` + `apps/frontend/schema/generated/companies.ts`.

### 2.3 Operator authentication dependency (small unblocker)

`get_current_operator` does not yet exist. It needs to:

1. Run after `get_current_user` (Supabase JWT verification — already implemented).
2. Look up `operators` row by `supabase_user_id`.
3. `403` if missing or `status != "active"`.

It belongs in `app/core/auth.py` next to `get_current_user`. Reference pattern: §4 of `docs/conventions/backend-patterns.md` (recruiter resolution from authenticated user).

### 2.4 Seed script for synthetic operators / companies

Mirror `seed_recruiters.py` — `seed_operators.py` (~5 operators) + `seed_companies.py` (~10 companies across `CompanyStage` distribution). Without seeded operators the new operator-auth dependency cannot be exercised end-to-end in dev.

### 2.5 What is **not** required yet

Out of scope until the workflow itself is built; do not block on these:

- Hiring-manager portal UI (Next.js).
- Operator portal UI for the recruiter-assignment HITL queue (Agent 0 work).
- ATS integrations (Ashby / Lever) — explicitly out of PoW scope.
- Slack / email notifications on intake completion (deferred to Agent 0's notify step).

---

## 3. Trigger Surface

### 3.1 Intake API trigger (post-prereqs)

**Endpoint:** `POST /api/v1/jobs/intake`
**File (planned):** `apps/backend/app/api/v1/endpoints/jobs.py`

| Field | Value |
|---|---|
| Auth | `get_current_user` resolved to a `CompanyUser` row (similar to recruiter resolution in `candidates.py`); falls back to `get_current_operator` for operator-on-behalf-of submissions |
| Path | `POST /api/v1/jobs/intake` |
| Body | `JobIntakeRequest` — see §3.2 |
| Response | `202 Accepted` — `{job_id, workflow_id, status: "intake"}` |
| Error modes | `404` company missing; `403` user not seated at company; `422` validation; `429` rate limit (per CLAUDE.md new-API checklist) |
| Workflow ID | `job-intake-{job_id}` |
| Reuse policy | `WorkflowIDReusePolicy.REJECT_DUPLICATE` — once a Job has an intake workflow, a re-submit must go through HITL #2 reeval, not a second intake |
| Invocation | `client.start_workflow(...)` (fire-and-forget, **not** blocking) — full lifecycle measured in days, frontend uses SSE/polling |

The endpoint must:

1. Create the `Job` row with `status="intake"`, `workflow_id=f"job-intake-{job_id}"`, raw intake fields populated, `must_have_skills`/`role_category` left null (workflow fills them).
2. Start the workflow with the new `job_id` as input.
3. Return immediately — full pipeline (intake → recruiter assignment → sourcing → scoring → review) is multi-day, not multi-second.

Rate limiting (per CLAUDE.md): keyed on `(company_id, hour)` with a sane cap (e.g. 10 intakes/hour/company) — companies do not legitimately submit 100 roles in a minute.

### 3.2 `JobIntakeRequest` shape

```python
class JobIntakeRequest(BaseModel):
    company_id: UUID
    title: str  # 1-200 chars
    jd_text: str  # full JD body, 1-20000 chars
    intake_notes: str | None  # operator's onboarding-call notes
    remote_onsite: RemoteOnsite | None
    location_text: str | None  # freeform
    compensation_min: int | None  # USD, validated min < max
    compensation_max: int | None
    extra: dict | None  # pass-through for future fields
```

`role_category`, `seniority_level`, `stage_fit`, `must_have_skills`, `nice_to_have_skills` are intentionally **not** request fields — they are `classify_role_type` outputs (§4.1). Letting the LLM extract them avoids a manual step in the operator's intake form and matches the docs' design (§10 master doc).

### 3.3 Workflow ID conventions

| Trigger | Workflow ID format | Example |
|---|---|---|
| Intake API | `job-intake-{job_id}` | `job-intake-7f3a8c0e-…` |
| Seed script (later) | `seed-job-{slug}-{i}` | `seed-job-founding-engineer-3` |

Same task queue (`converio-queue`) as indexing workflows.

---

## 4. Activity Contracts

Three activities live under `apps/backend/app/temporal/product/job_intake/activities/` (planned). Worker auto-discovery via `@ActivityRegistry.register("job_intake", "<name>")` matching the candidate / recruiter pattern.

### 4.1 `classify_role_type`

| Field | Value |
|---|---|
| Inputs | `{title, jd_text, intake_notes}` |
| Outputs | `RoleClassification` — `{role_category: RoleCategory, seniority_level: Seniority, stage_fit: CompanyStage \| None, remote_onsite: RemoteOnsite \| None, must_have_skills: list[str], nice_to_have_skills: list[str], rationale: str}` |
| LLM | Yes — gemini-2.0-flash via OpenRouter (per `docs/contrario_proof_of_work_context.md` §18); structured output / function calling |
| Failure modes | LLM returns invalid enum → activity raises `ValueError` after 1 retry (caught by Temporal retry policy) |
| Idempotency | Deterministic for fixed prompt — but LLM nondeterminism means re-runs may yield different `must_have_skills` ordering. Sort + dedupe before return so replay-safe equality holds at the workflow boundary. |
| Retry | `_LLM_RETRY` (max=3, backoff=2× exponential) — same shape as scorecard agent will use later |

Prompt skeleton (deterministic, assembled in activity):

```
SYSTEM: You classify recruiting role intakes for a managed recruiting service.
Return strict JSON matching the RoleClassification schema. Use only the enum
values listed below; if uncertain, pick the closest.
Enums: RoleCategory={engineering|gtm|design|ops|data}; Seniority={...}; ...

USER: Title: {title}
JD:
{jd_text}
Intake notes:
{intake_notes or "(none)"}
```

User input (`title`, `jd_text`, `intake_notes`) is delimited; system instructions stay in a separate role per CLAUDE.md AI/LLM rules (no raw user input in privileged prompts). Output validated against `RoleClassification` Pydantic model before return.

### 4.2 `generate_evaluation_rubric`

| Field | Value |
|---|---|
| Inputs | `RoleClassification`, `intake_notes`, `extra` (any company-side rubric hints) |
| Outputs | `EvaluationRubric` — `{dimensions: list[RubricDimension], rationale: str}` where `RubricDimension = {name: str, description: str, weight: float (0-1), evaluation_guidance: str}` |
| LLM | Yes — gemini-2.0-flash, structured output |
| Failure modes | Weights don't sum to ~1.0 → activity normalizes (renormalize to 1.0, warn-log if drift > 0.05); empty dimensions → raise; >12 dimensions → truncate to top-12 by weight |
| Idempotency | Replay-safe by deterministic sort: dimensions ordered by `(-weight, name)` before return |
| Retry | `_LLM_RETRY` |

Constraint: at least 4, at most 8 dimensions per rubric (the docs' "Founding Engineer" example uses 6). Activity asserts in [4, 8] post-truncate.

Example dimensions (from §10 of master doc, for a "Founding Engineer" role):

```
distributed_systems_depth (0.25)
full_stack_ownership      (0.20)
startup_stage_fit         (0.20)
open_source_signals       (0.15)
communication_clarity     (0.10)
system_design_thinking    (0.10)
```

The LLM gets the role classification + intake notes — never the candidate pool. The rubric is candidate-agnostic by design (Agent 4 applies it to every candidate identically).

### 4.3 `persist_job_record`

| Field | Value |
|---|---|
| Inputs | `job_id`, `RoleClassification`, `EvaluationRubric` |
| Outputs | `{job_id, rubric_id, rubric_version: 1}` |
| Dependencies | `JobRepository.update`, `RubricRepository.create`, `async_session_maker()` |
| Failure modes | Job row missing → raise (intake API guarantees existence — failure is real bug); FK violation if `rubric.job_id` doesn't match → raise |
| Idempotency | UPSERT semantics on Job (only fills classification fields if currently null); `(job_id, version)` unique on Rubric prevents duplicate inserts on replay (Temporal will retry, second insert fails harmlessly — wrap in `try/except IntegrityError`) |
| Retry | `_DB_RETRY` (max=3, backoff=1.5×) |

Updates Job: `role_category`, `seniority_level`, `stage_fit`, `remote_onsite` (if not already set on intake), `must_have_skills`, `nice_to_have_skills`, `status="recruiter_assignment"` (transition from `intake`).

Inserts Rubric: `version=1`, `dimensions=[...]`. Future versions (HITL #2 reeval) bump `version` and create a new row, never UPDATE.

---

## 5. Workflow Skeleton

```python
# apps/backend/app/temporal/product/job_intake/workflows/job_intake_workflow.py (planned)

@workflow.defn(name="JobIntakeWorkflow")
class JobIntakeWorkflow:
    @workflow.run
    async def run(self, raw: dict) -> dict:
        inp = JobIntakeInput.model_validate(raw)

        # Phase A — classification + rubric
        classification = await workflow.execute_activity(
            "job_intake.classify_role_type",
            {"title": inp.title, "jd_text": inp.jd_text, "intake_notes": inp.intake_notes},
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_LLM_RETRY,
        )
        rubric = await workflow.execute_activity(
            "job_intake.generate_evaluation_rubric",
            {"classification": classification, "intake_notes": inp.intake_notes,
             "extra": inp.extra},
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_LLM_RETRY,
        )
        persisted = await workflow.execute_activity(
            "job_intake.persist_job_record",
            {"job_id": inp.job_id, "classification": classification, "rubric": rubric},
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=_DB_RETRY,
        )

        # Phase B — Agent 0 (recruiter assignment + HITL #1 pause)
        # Blocking child workflow: returns only after operator confirms.
        assignment_result = await workflow.execute_child_workflow(
            "RecruiterAssignmentWorkflow",
            {"job_id": inp.job_id, "classification": classification},
            id=f"recruiter-assignment-{inp.job_id}",
            task_queue="converio-queue",
        )

        # Phase C — wait for recruiter submissions, score in parallel, rank
        # Implemented as a dynamic loop driven by submission Signals (§8.2).
        ranking = await self._await_submissions_and_score(inp.job_id, rubric, assignment_result)

        # Phase D — HITL #2 (company review)
        company_decision = await self._await_company_review(inp.job_id)

        return {
            "job_id": inp.job_id,
            "rubric_version": persisted["rubric_version"],
            "assigned_recruiters": assignment_result["confirmed_recruiter_ids"],
            "shortlist_count": len(ranking["shortlist"]),
            "company_decision": company_decision,
        }
```

Pseudo-code only — `_await_submissions_and_score` and `_await_company_review` are Signal-driven loops covered in §8.

---

## 6. State Transitions on `jobs.status`

```
intake                        ← row created by API on submission
   ↓ persist_job_record
recruiter_assignment          ← Agent 0 ranking pending operator approval
   ↓ operator_approval Signal
sourcing                      ← recruiters notified, submissions trickling in
   ↓ first submission scored
scoring                       ← scorecards generating in parallel
   ↓ ranking complete
review                        ← HITL #2 — awaiting company decision
   ↓ company_review Signal (terminal)
closed
```

Each transition is a `JobRepository.update` inside an activity (never inside the workflow itself — workflow stays deterministic). `WorkflowRun` rows mirror these for FE polling fallback.

---

## 7. Decisions Log

- **D1 — Job Intake is fire-and-forget at the HTTP layer.** Unlike recruiter indexing (~3s blocking `execute_workflow`), the full intake-to-shortlist arc is multi-day. The endpoint returns `202 Accepted` with `workflow_id` and the FE subscribes via SSE.
- **D2 — Classification fields live on `Job`, rubric on `Rubric`.** Separate tables because rubric is **versioned** (HITL #2 reeval bumps `version`) but classification is one-shot. Avoids a `rubrics`-vs-`jobs` denormalization fight when reeval lands.
- **D3 — `WorkflowIDReusePolicy.REJECT_DUPLICATE`.** A second intake for the same Job is always a bug; reeval flows through HITL #2 → `RankingAgentWorkflow` re-execution, not a re-run of intake.
- **D4 — Agent 0 is a child workflow, not an inline activity chain.** Two reasons: (a) Agent 0 owns its own HITL Signal — embedding that in `JobIntakeWorkflow` would require splitting the parent into pre- and post-Signal halves; (b) child workflow gives independent Temporal event history per agent, easier to debug.
- **D5 — Sourcing Agent (Agent 3) is fallback-only, not always spawned.** `JobIntakeWorkflow` only fires `SourcingAgentWorkflow` if Agent 6 (Ambient Monitor) flags the pool as thin. The recruiter-submission path is primary; sourcing is the safety net. Saves cost and keeps the happy path simple.
- **D6 — Rubric weights normalize, not validate-and-fail.** LLMs occasionally return weights that sum to 0.97 or 1.04. Renormalizing is friendlier than 500-ing the workflow; drift > 0.05 is logged so degraded output is observable.
- **D7 — Operator-only company creation for the PoW.** Real Converio onboards via a sales/onboarding call — no self-serve company signup. Mirroring this in the proof-of-work means the operator endpoint is the only company-write path. A future `/onboard` magic-link flow is out of scope.
- **D8 — `intake_notes` is free-text and goes into both LLM activities.** `classify_role_type` uses it for skill extraction; `generate_evaluation_rubric` uses it for tuning weights (e.g. operator notes "team is small, generalist preferred" should reduce `distributed_systems_depth` weight). Same content; two prompts. Cheaper than a third "summarize intake" LLM call.

---

## 8. Downstream Orchestration

`JobIntakeWorkflow` is the parent for everything that follows. Sequencing matters because each phase produces inputs the next phase consumes.

### 8.1 Phase B — Agent 0 (Recruiter Assignment + HITL #1)

```
parent: JobIntakeWorkflow
  └── child: RecruiterAssignmentWorkflow  (blocking await)
        ├── activity: search_recruiter_pool        (Neo4j Cypher)
        ├── activity: score_recruiter_fit          (LLM)
        ├── activity: rank_and_select_recruiters   (deterministic)
        ├── activity: format_recruiter_recommendations
        ├── workflow.wait_for_signal("operator_approval")   ← HITL #1
        ├── activity: assign_recruiters_to_role    (PG + Neo4j ASSIGNED_TO edges)
        └── activity: notify_assigned_recruiters   (Slack/email)
```

The parent **blocks** until operator approval comes through — this is the simplest implementation of HITL #1 and matches §11 of the master doc. Total wall time: minutes to hours (operator latency).

Output to parent: `{confirmed_recruiter_ids: [...], assignment_ids: [...]}`. Parent uses these to gate Phase C (no recruiter, no submissions).

### 8.2 Phase C — Submission-driven scoring + ranking

This is the **dynamic** phase. Recruiters submit candidates over hours/days, each via the Recruiter Portal `POST /api/v1/jobs/{job_id}/submissions` endpoint (planned). Each submission Signals the parent workflow:

```
parent: JobIntakeWorkflow
  ├── workflow.set_signal_handler("candidate_submitted")  ← appends to in-memory list
  ├── workflow.wait_condition(lambda: submissions_window_closed)
  │     # closed when: recruiter sets done | timeout (e.g. 7 days) | min N reached
  ├── for each submission:
  │     └── child: CandidateIndexingWorkflow(candidate_id)   (parallel)
  ├── parallel for each indexed candidate:
  │     └── child: ScorecardGeneratorWorkflow(job_id, candidate_id)
  └── child: RankingAgentWorkflow(job_id)
```

Two Signal-driven primitives in play:

- `candidate_submitted` Signal — recruiter portal endpoint signals the parent with `{candidate_id, submission_id, recruiter_id}`. Parent appends to `pending_submissions` list, kicks off `CandidateIndexingWorkflow` immediately (so indexing runs in parallel with further submissions).
- `submissions_done` Signal — operator or recruiter manually closes the window when "enough" candidates have been submitted, OR `wait_condition` times out.

**Why this design:** lets candidates trickle in without forcing the parent into an awkward poll loop. Each `CandidateIndexingWorkflow` runs fully in parallel; `ScorecardGeneratorWorkflow` per candidate runs in parallel; only `RankingAgentWorkflow` synchronizes (it consumes all scorecards).

`asyncio.gather()` over `execute_child_workflow(...)` calls handles the parallelism — Temporal's deterministic replay correctly records the `gather` outcome by completion order.

### 8.3 Phase C-bis — Sourcing Agent fallback

Triggered **only** if Agent 6 (Ambient Monitor) signals the parent that the pool is thin (size < 10, avg_score < 65, or candidate_age > 7d). Spawns `SourcingAgentWorkflow(job_id, rubric)` as a child. That workflow runs its own multi-store search → LLM gate → GitHub fallback loop, enqueues new candidates as their own `CandidateIndexingWorkflow` children, and re-feeds the parent's `candidate_submitted` Signal queue.

This keeps Agent 3 **opt-in** rather than always-on, matching D5.

### 8.4 Phase D — HITL #2 (Company Review)

```
parent: JobIntakeWorkflow
  ├── (transition jobs.status → "review")
  ├── workflow.wait_for_signal("company_review")   ← HITL #2
  └── handle decision:
        approve → mark CandidateSubmission status, push to ATS shortlist
        reject  → mark, optionally trigger reeval if rubric_override provided
        reeval  → bump rubric version, re-run ScorecardGenerator + Ranking on full pool
```

Same Temporal primitive as HITL #1 — `workflow.wait_for_signal`. Demonstrating the same architectural pattern at both matching boundaries is the explicit "this generalizes" demo in the master doc §15 / §19 D4.

The reeval branch re-enters Phase C (subset) for the new rubric version. It does **not** re-run intake/classification — that's already done.

### 8.5 Phase E — Ambient Monitor (Agent 6, scheduled, independent)

`AmbientMonitorWorkflow` is **not** a child of `JobIntakeWorkflow` — it's a separate Temporal **Schedule** (cron-style, every 24h). For each open Job, it:

1. Reads pool health via PG (deterministic, zero LLM — D6 of master doc).
2. Sends `pool_thin` Signal to the parent if thresholds breach → triggers Phase C-bis.
3. Sends `all_rejected` alert Signal → surfaces to FE.

Lives in `apps/backend/app/temporal/product/ambient_monitor/`. Not part of `JobIntakeWorkflow`'s code path — but the parent registers a Signal handler for `pool_thin` / `all_rejected`.

### 8.6 Orchestration diagram

```
                    POST /api/v1/jobs/intake
                              │
                              ▼
             ┌──────── JobIntakeWorkflow ────────┐
             │                                   │
             │  classify_role_type → rubric →    │
             │  persist_job_record                │
             │                                   │
             │  ┌─────────────────────────────┐  │
             │  │ RecruiterAssignmentWorkflow │  │  blocking
             │  │   ↳ HITL #1 operator_approval │  │  (HITL Signal)
             │  └─────────────────────────────┘  │
             │                                   │
             │  Signal loop: candidate_submitted │
             │    fan-out CandidateIndexing ─┐   │  parallel
             │    fan-out ScorecardGenerator ┤   │  parallel
             │  RankingAgentWorkflow ────────┘   │
             │                                   │
             │  ┌─────────────────────────────┐  │
             │  │ HITL #2 company_review       │  │  (HITL Signal)
             │  └─────────────────────────────┘  │
             │                                   │
             └──────── status: closed ───────────┘

   parallel:                                Schedule (24h):
   SourcingAgentWorkflow (Phase C-bis)      AmbientMonitorWorkflow
   ↑ on demand from Ambient Monitor         → signals parent
```

---

## 9. Verification Plan (post-implementation)

### Local end-to-end (after build)

```bash
# 1. Apply migrations
cd apps/backend && alembic upgrade head

# 2. Bring up infra + worker
docker compose up postgres neo4j temporal -d
uv run python -m app.temporal.worker

# 3. Seed prerequisites
uv run python scripts/seed_recruiters.py --limit 25
uv run python scripts/seed_operators.py
uv run python scripts/seed_companies.py

# 4. Create company + hiring-manager seat (operator JWT)
curl -X POST localhost:8000/api/v1/companies \
  -H "Authorization: Bearer $OPERATOR_JWT" \
  -d '{"name":"Stripe (test)","stage":"growth"}'

curl -X POST localhost:8000/api/v1/companies/$COMPANY_ID/users \
  -H "Authorization: Bearer $OPERATOR_JWT" \
  -d '{"email":"hm@stripe.test","role":"hiring_manager"}'

# 5. Submit intake (hiring-manager JWT)
curl -X POST localhost:8000/api/v1/jobs/intake \
  -H "Authorization: Bearer $HM_JWT" \
  -d '{"company_id":"...","title":"Founding Engineer",
       "jd_text":"...","intake_notes":"small team, generalist"}'
# → 202 with {job_id, workflow_id, status: "intake"}

# 6. Check Temporal UI for JobIntakeWorkflow → blocked at RecruiterAssignmentWorkflow's
#    wait_for_signal("operator_approval")
```

### Idempotency / replay

- Re-running intake activities under Temporal replay must produce identical `Rubric.dimensions` (deterministic sort enforces this).
- Replay test in `apps/backend/tests/temporal/test_workflow_replay.py` (extend post-build).

### Hermetic tests (planned scope)

- `test_classify_role_type.py` — mocked LLM, asserts Pydantic validation, enum coercion, sort/dedupe of skill arrays.
- `test_generate_evaluation_rubric.py` — weight normalization, dimension count bounds, deterministic sort.
- `test_persist_job_record.py` — UPSERT semantics on Job, version=1 INSERT on Rubric, IntegrityError swallowed on replay.
- `test_job_intake_workflow.py` — full workflow with mocked activities, asserts state transitions on `jobs.status`.

---

## 10. Out of Scope (for the Job Intake PR)

These are required for the **end-to-end** product but are intentionally excluded from the Job Intake workflow PR to keep it shippable:

- `RecruiterAssignmentWorkflow` (Agent 0) — separate PR. Job Intake just registers the child-workflow call site.
- `ScorecardGeneratorWorkflow` (Agent 4) — separate PR.
- `RankingAgentWorkflow` (Agent 5) — separate PR.
- `SourcingAgentWorkflow` (Agent 3) — separate PR, fallback-only.
- `AmbientMonitorWorkflow` (Agent 6) — separate PR, scheduled.
- HITL UIs (Operator queue, Company shortlist review).
- ATS push (Ashby/Lever) on `approve` — explicitly out of PoW scope.
- Slack notifications on intake completion.

The Job Intake PR ships: prereqs (§2.2–§2.4), 3 activities, the workflow up through `persist_job_record` + a stub child-workflow call that returns immediately for now, the `POST /jobs/intake` endpoint, schemas, tests.

---

## 11. Cross-References

**Context:**

- `docs/contrario_proof_of_work_context.md` §10 (Phase 2 — Job Intake), §11 (Agent 0), §15 (HITL), §19 (Decisions D1, D4, D6, D8, D9)
- `docs/contrario_architecture_corrections.md` — managed-service framing, two HITL points
- `docs/contrario_data_model_from_screenshots.md` — original `companies` / `jobs` field derivation

**Sibling design docs:**

- `docs/recruiter_indexing_workflow.md` — same format; reference for activity contracts, retry policies, decisions log shape.
- `docs/plans/agent_2_candidate_indexing_plan.md` + `docs/plans/agent_2_flow.md` — candidate-side mirror.

**Conventions:**

- `docs/conventions/openapi-workflow.md` — spec → codegen pipeline (must be followed for `companies.json` + `jobs.json`).
- `docs/conventions/backend-patterns.md` — repository / service / Temporal activity registration patterns.

**Code paths (planned, all under `apps/backend/`):**

- Workflow: `app/temporal/product/job_intake/workflows/job_intake_workflow.py`
- Activities: `app/temporal/product/job_intake/activities/{classify_role_type,generate_evaluation_rubric,persist_job_record}.py`
- API: `app/api/v1/endpoints/{companies,jobs}.py`
- Specs: `app/api/v1/specs/{companies,jobs}.json`
- Generated schemas: `app/schemas/generated/{companies,jobs}.py`
- Product schemas: `app/schemas/product/job.py`
- Auth: extend `app/core/auth.py` with `get_current_operator`, `get_current_company_user`
- Seed: `scripts/{seed_operators,seed_companies}.py`
- Tests: `tests/temporal/activities/test_{classify_role_type,generate_evaluation_rubric,persist_job_record}.py`, `tests/temporal/test_job_intake_workflow.py`, `tests/api/test_companies_endpoints.py`, `tests/api/test_jobs_endpoints.py`

---

*Design doc — captures requirements before implementation. Update once code lands and replace forward-looking sections with implemented behavior, mirroring `docs/recruiter_indexing_workflow.md`.*
