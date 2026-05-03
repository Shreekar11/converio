# Self-Serve Auth — Implementation Walkthrough

Reference document for the self-serve authentication subsystem. Reflects what
ships on `feat/job-intake-workflow` after the self-serve auth work landed.

Source files:
- `apps/backend/app/api/v1/endpoints/auth.py`
- `apps/backend/app/api/v1/endpoints/companies.py`
- `apps/backend/app/api/v1/endpoints/jobs.py`
- `apps/backend/app/core/auth.py`
- `apps/backend/app/core/rate_limit.py`
- `apps/backend/app/repositories/company_users.py`
- `apps/backend/app/repositories/recruiters.py`
- `apps/backend/app/repositories/companies.py`
- `apps/backend/app/schemas/enums.py`
- `apps/backend/app/api/v1/specs/auth.json`
- `apps/backend/alembic/versions/0004_company_pending_review_status.py`

---

## 1. Overview

Before this change every Converio tenant was provisioned by an internal
operator: companies were inserted directly via the operator-only
`POST /companies` endpoint, hiring-manager seats were pre-created with
`POST /companies/{id}/users` (with `supabase_user_id = null`), and recruiters
were provisioned out-of-band.

The self-serve auth subsystem opens two public sign-up paths so the platform
can grow without operator-in-the-loop for every new tenant, while preserving
the operator approval gate that keeps untrusted companies out of the active
matching pool.

Two actor paths land in the system:

- **Company (hiring side)** — a hiring leader signs in with Supabase, calls
  `POST /auth/company/signup`, and gets seated as the `admin` of a freshly
  created company in `pending_review`. The company cannot submit job intakes
  until an operator promotes it to `active` via
  `PATCH /companies/{id}/status`.
- **Recruiter (sourcing side)** — a recruiter signs in with Supabase, calls
  `POST /auth/recruiter/signup`, and is created in `pending` status. The next
  step (out of scope for the API, owned by the existing
  `RecruiterIndexingWorkflow`) is to call `POST /recruiters/{id}/index` to
  trigger graph + vector indexing; the operator activates the recruiter on
  successful indexing review.

The operator approval gate is enforced in two places:

1. New self-serve companies start in `CompanyStatus.PENDING_REVIEW`
   (Alembic migration `0004` flipped the column's `server_default`).
2. `POST /jobs/intake` now requires `get_current_active_company_user` —
   `pending_review`, `paused`, and `churned` companies all receive a 403
   before any DB IO.

---

## 2. Auth Model

### JWT verification

The `HTTPBearer` security extractor in `app.core.auth` pulls the bearer
token off the request, hands it to the existing `jwt_verifier.verify_token`
(Supabase JWKS-backed), and constructs `CurrentUser` from the verified
claims:

```python
class CurrentUser(BaseModel):
    id: str          # Supabase `sub`
    email: str | None
    role: str = "user"
    app_metadata: dict
    user_metadata: dict
```

`get_current_user` is the only dependency that touches the JWT. Every other
auth dependency in the system layers on top of it.

### The four FastAPI dependencies

| Dependency | Returns | Purpose | Failure mode |
|-----------|---------|---------|--------------|
| `get_current_user` | `CurrentUser` | Verified Supabase identity. No DB lookup. | `401` on missing/invalid token. |
| `get_current_company_user` | `CompanyUser` | Resolves `sub` to a seat row regardless of company lifecycle. | `403 "Not a company user"` if no seat. |
| `get_current_recruiter` | `Recruiter` | Resolves `sub` to a non-suspended recruiter row. | `403 "Not a recruiter"` (no row) or `403 "Recruiter account suspended"`. |
| `get_current_active_company_user` | `CompanyUser` | Layered on `get_current_company_user`; additionally enforces `Company.status == active`. Re-fetches the company row on every request to avoid stale ORM snapshots. | `403 "Company not found"` (race) or `403 "Company not active"`. |

`get_current_operator` (already present) follows the same pattern and
collapses "no operator row" and "inactive operator" into a single 403 to
avoid leaking which Supabase users are Converio operators.

### Role resolution precedence

`/auth/me` walks the role tables in this fixed order and short-circuits on
the first match:

1. **Operator** — by `supabase_user_id`. Highest precedence so an operator
   who happens to also seat themselves on a test company never resolves as
   `company_user`.
2. **CompanyUser** — by `supabase_user_id`.
3. **Recruiter** — by `supabase_user_id`.
4. **Email backfill** — only after every `supabase_user_id` lookup misses,
   and only if the JWT carries an `email` claim. Looks for a
   pre-provisioned company seat with `supabase_user_id IS NULL` and links
   it.
5. **Unregistered** — terminal state. The frontend uses this to route into
   the self-serve sign-up wizard.

### Authority rule

> The role of the caller is `JWT.sub + DB state`. It is never `URL param`,
> never `request body`, and never `query string`.

In practice this means:

- `company_signup` derives `email` from `current_user.email`. The request
  body has no `email` field.
- `submit_job_intake` derives `company_id` from `company_user.company_id`.
  The body still carries a `company_id` for spec-compatibility, but its
  value is **ignored** so a seated user cannot submit intakes against an
  arbitrary company.
- `recruiter_signup` ignores any `email` on the payload — the JWT email is
  authoritative.

---

## 3. Company Onboarding Flow

### Endpoint signature

```
POST /api/v1/auth/company/signup
Authorization: Bearer <supabase-jwt>
Content-Type: application/json
```

Body: `CompanySignupRequest` (generated from `auth.json`). Required field:
`name`. Optional: `stage`, `industry`, `website`, `logo_url`,
`company_size_range`, `founding_year`, `hq_location`, `description`.

Returns `201 CompanyDetailResponse` (company + seated users array) or one
of the conflict shapes below.

### Validation pipeline (in execution order)

1. **Email-claim guard** — if `current_user.email is None` (anonymous /
   phone-only Supabase sign-in), return `400 "Email claim required for
   self-serve signup"`. A self-serve tenant must have an email.
2. **Per-`sub` rate limit** — `signup_rate_limiter.check(current_user.id)`.
   On miss, returns `429` with `Retry-After: 3600` header.
3. **Cross-role email uniqueness** — sequential lookups against the
   operator and recruiter tables. Each conflict returns `409 "Email
   already in use"` with a distinguishing `X-Error-Code` header
   (`email_in_use_operator` or `email_in_use_recruiter`).
4. **Already-onboarded short-circuit** — if a `CompanyUser` already exists
   for this `sub`, return `409 "Already onboarded as company user"` with
   `X-Error-Code: already_onboarded`. Re-running signup never creates a
   second tenant.
5. **Pre-provisioned seat link path** — if a `CompanyUser` row exists for
   this email with `supabase_user_id IS NULL` (operator pre-seated),
   `link_supabase_user_id` binds the auth identity and the endpoint
   returns the linked seat instead of creating a new company.
6. **Standard self-serve path** — case-insensitive duplicate-name guard
   (`get_by_name_ci`), then `CompanyRepository.create(...,
   status=CompanyStatus.PENDING_REVIEW)`, then `CompanyUserRepository.create(
   role=CompanyUserRole.ADMIN, supabase_user_id=current_user.id)`.

### CompanyStatus lifecycle

`CompanyStatus` (in `app/schemas/enums.py`) is the lifecycle enum:

| Value | Semantics |
|-------|-----------|
| `pending_review` | New self-serve signup. Cannot submit job intakes. Awaiting operator review. |
| `active` | Approved by operator. May submit job intakes. |
| `paused` | Temporarily disabled. Cannot submit intakes. Re-activatable. |
| `churned` | Terminal. Off-boarded. Not re-activatable — re-onboarding requires a new company row to keep audit trails intact. |

### PATCH /api/v1/companies/{company_id}/status

Operator-only (gated by `get_current_operator`). Body:
`CompanyStatusUpdate { status: CompanyStatus }`.

The allowed-transition matrix is centralised in
`VALID_STATUS_TRANSITIONS` at the top of
`apps/backend/app/api/v1/endpoints/companies.py`:

| From \ To       | active | paused | churned |
|-----------------|--------|--------|---------|
| `pending_review` | yes    | no     | yes     |
| `active`         | —      | yes    | yes     |
| `paused`         | yes    | —      | yes     |
| `churned`        | no     | no     | —       |

Anything not in that table returns `422 "Invalid status transition"`.
404 if the company id does not exist. Audit log emits `from_status` +
`to_status` keyed by operator and company ids — never PII.

### POST /api/v1/jobs/intake — now gated

Previously the dependency was `get_current_company_user`. It is now
`get_current_active_company_user`, which means:

- `CompanyStatus.PENDING_REVIEW` → `403 "Company not active"`
- `CompanyStatus.PAUSED` → `403 "Company not active"`
- `CompanyStatus.CHURNED` → `403 "Company not active"`
- `CompanyStatus.ACTIVE` → proceed

The `company_id` for the inserted `Job` row and the `JobIntakeWorkflow`
input is taken from `company_user.company_id`. The body still carries a
`company_id` field for OpenAPI-spec compatibility; the value is ignored.

### Pre-provisioned seat backfill

This is the bridge between the legacy operator-driven flow and self-serve:

1. Operator calls `POST /companies/{id}/users` with `email` and `role`.
   The seat row lands with `supabase_user_id = NULL`.
2. The seated employee signs in with Google (or any Supabase identity
   provider) using the same email.
3. The frontend calls `GET /auth/me`. The handler walks precedence:
   operator (miss) → company_user-by-sub (miss) → recruiter (miss) →
   **email backfill**: `company_user_repo.get_by_email(jwt.email)` returns
   the pre-provisioned seat with null `supabase_user_id`, and
   `link_supabase_user_id(seat.id, jwt.sub)` binds the auth identity.
4. `/auth/me` returns the linked seat as `role: "company_user"` with the
   company's current `onboarding_state.company_status`.

The same backfill path also fires inside `company_signup` (branch 5),
covering the case where the user calls signup before `/auth/me`.

---

## 4. Recruiter Onboarding Flow

### Endpoint signature

```
POST /api/v1/auth/recruiter/signup
Authorization: Bearer <supabase-jwt>
Content-Type: application/json
```

Body: `RecruiterSignupRequest`. Required: `full_name`, `domain_expertise`
(non-empty `list[str]`). Optional: `workspace_type`,
`recruited_funding_stage`, `bio`, `linkedin_url`. Returns `201
RecruiterResponse`.

### Validation pipeline

Mirrors the company flow:

1. **Email-claim guard** — `400` if `current_user.email is None`.
2. **Per-`sub` rate limit** — same `signup_rate_limiter` (5 / hour). `429`
   on miss.
3. **Cross-role email uniqueness** — checks `Operator` then `CompanyUser`.
   `409 "Email already in use"` with `X-Error-Code: email_in_use_operator`
   or `email_in_use_company`.
4. **Already-onboarded short-circuit** —
   `RecruiterRepository.get_by_supabase_id(sub)`. `409 "Already registered
   as recruiter"` with `X-Error-Code: already_onboarded`.
5. **Create the recruiter** in `RecruiterStatus.PENDING`. The
   `domain_expertise` field arrives as `list[DomainExpertiseItem]` (a
   RootModel wrapping a non-empty string); `.root` is unwrapped per item
   so the repo receives a plain `list[str]`.
6. **Project + return** — `_project_recruiter` projects the row to the
   `/auth/me`-style profile dict and returns it inside the API envelope.

### RecruiterStatus lifecycle

| Value | Semantics |
|-------|-----------|
| `pending` | New self-serve signup. Not eligible for matching. Mid-indexing or awaiting operator review. |
| `active` | Indexed and approved. Eligible for `RecruiterIndexingWorkflow` outputs and matching. |
| `suspended` | Blocked. `get_current_recruiter` returns `403 "Recruiter account suspended"`. |

`get_current_recruiter` accepts both `pending` and `active` (so a recruiter
mid-onboarding can still hit recruiter-facing endpoints).

### Next step after signup

The recruiter signup endpoint **does not** start the indexing workflow.
The frontend should call `POST /api/v1/recruiters/{id}/index` after a
successful signup; that endpoint is owned by the existing
`RecruiterIndexingWorkflow` (see `docs/recruiter_indexing_workflow.md`).
The operator promotes `pending` → `active` from the operator console once
indexing review is clean.

---

## 5. Identity Resolver: GET /auth/me

```
GET /api/v1/auth/me
Authorization: Bearer <supabase-jwt>
```

Always returns `200`. The handler never raises on identity-lookup miss —
`unregistered` is a valid terminal state used by the frontend to route
into the sign-up wizard.

### Resolution order

1. Operator by `sub` → returns `role: "operator"`, profile from
   `_project_operator`.
2. CompanyUser by `sub` → fetches linked Company, returns
   `role: "company_user"` with `onboarding_state.company_status`.
3. Recruiter by `sub` → returns `role: "recruiter"` with
   `onboarding_state.recruiter_status`.
4. Email backfill (only if `current_user.email is not None` and steps 1-3
   missed) → links the pre-provisioned seat and returns
   `role: "company_user"`.
5. Fall-through → `role: "unregistered"`, `profile: null`,
   `onboarding_state: null`.

### Response shapes (envelope omitted)

**Operator**:
```json
{
  "role": "operator",
  "profile": { "id": "...", "email": "...", "full_name": "...", "status": "active", "created_at": "...", "updated_at": "..." },
  "onboarding_state": null
}
```

**Company user** (note `profile` is nested with both `user` and `company`
so the frontend can render company branding without a second round-trip):
```json
{
  "role": "company_user",
  "profile": {
    "user":    { "id": "...", "company_id": "...", "email": "...", "role": "admin",     "...": "..." },
    "company": { "id": "...", "name": "...", "status": "pending_review", "...": "..." }
  },
  "onboarding_state": { "company_status": "pending_review" }
}
```

**Recruiter**:
```json
{
  "role": "recruiter",
  "profile": { "id": "...", "email": "...", "full_name": "...", "status": "pending", "domain_expertise": ["fintech"], "...": "..." },
  "onboarding_state": { "recruiter_status": "pending" }
}
```

**Unregistered**:
```json
{ "role": "unregistered", "profile": null, "onboarding_state": null }
```

### Pre-provisioned seat backfill — detail

The fourth branch in `/auth/me` is the only place where lookup happens by
email rather than `sub`. The flow:

1. `seated_user = company_user_repo.get_by_email(current_user.email)`.
2. Defensive check: `seated_user is not None and seated_user.supabase_user_id is None`.
   If a row exists with a non-null `supabase_user_id` that does **not**
   match `sub`, we do **not** touch it — that would be cross-account
   takeover. The path is the email match × null-supabase-id intersection
   only.
3. `linked = company_user_repo.link_supabase_user_id(seated_user.id, sub)`.
   The repo issues a Core `UPDATE` and commits inside the call.
4. Re-fetch the company; if it is missing (orphaned seat) log
   `seat_backfilled` with ids, fall through to `unregistered` so the FE
   can recover gracefully.
5. Otherwise return the linked seat as a normal `company_user` response.

Audit log emits `event=seat_backfilled` with `user_id`,
`company_user_id`, and `company_id` only. Email is never logged.

---

## 6. Security Properties

### Cross-role email uniqueness

A single email can be at most one of operator / company_user / recruiter.
Both signup endpoints check the other two role tables before creating
their row. The recruiter table check on the company path (and the
company-user check on the recruiter path) is implicit through the
`already_onboarded` short-circuit on `sub`.

Because no role table has a DB-level unique constraint on `email`, this
contract is application-layer only. Two simultaneous signups for the same
email could theoretically race past the check and both succeed; the
`already_onboarded` guard on `sub` catches the more likely retry pattern,
and the cross-role uniqueness is a soft contract in this PR. A unique
index across role tables is a follow-up.

### Rate limiting

`signup_rate_limiter` (in `app.core.rate_limit`) is a
`SlidingWindowRateLimiter(max_requests=5, window_seconds=3600)` keyed by
Supabase `sub`. Both `/auth/company/signup` and
`/auth/recruiter/signup` consume the same bucket per identity, so the
limit is "5 signup attempts per identity per hour" across both endpoints.

On miss the handler returns `429 "Too many signup attempts; try again
later"` with a `Retry-After: 3600` header.

**Operational caveat (called out in `rate_limit.py`):** the limiter is
in-process and single-bucket-per-worker. With multiple uvicorn workers
the effective ceiling is `5 × worker_count`. Production should swap in
Redis-backed storage; the `check(key) -> bool` contract is trivially
swappable.

### JWT email is authoritative; payload email is ignored

`current_user.email` is the only source of email used at signup time.
Neither signup schema accepts `email` in the body. This prevents:

- A user with one Supabase identity claiming a tenant under a different
  email.
- A pre-provisioned seat being linked to the wrong identity (the email
  must match the JWT claim, not a value the client picks).

### Pre-provisioned seat backfill safety

Linking only fires when **all** of:

- `current_user.email is not None`,
- `company_user_repo.get_by_email(jwt.email)` returns a row,
- That row's `supabase_user_id IS NULL`.

If any of those is false, the backfill branch is skipped. There is no
path that overwrites a non-null `supabase_user_id`.

### Tenant scoping

`POST /jobs/intake` derives `company_id` from `company_user.company_id`,
not from the request body. The body's `company_id` field is retained for
OpenAPI compatibility and explicitly ignored. Same rule applies to every
other tenant-scoped operation: tenant identifiers come from the auth
context.

### Generic error responses

All sign-up failure modes return HTTPException with a short generic
`detail` string. Internal context (operator ids, company ids, error
strings) goes to structured logs only. The `X-Error-Code` headers expose
machine-readable conflict reasons so the frontend can route without
parsing free-text.

---

## 7. API Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/auth/me` | Bearer | Identity resolver. Returns role + profile + onboarding state. |
| `POST` | `/api/v1/auth/company/signup` | Bearer | Self-serve company registration. Creates company in `pending_review` and seats user as admin. |
| `POST` | `/api/v1/auth/recruiter/signup` | Bearer | Self-serve recruiter registration. Creates recruiter in `pending`. |
| `PATCH` | `/api/v1/companies/{id}/status` | Bearer (operator) | Operator approval / lifecycle transitions. |
| `POST` | `/api/v1/jobs/intake` | Bearer (active company user) | Job intake. Now requires company in `active` status. |

### GET /auth/me

- `200` — always (even for unregistered users).
- `401` — missing or invalid bearer token.

```bash
curl -H "Authorization: Bearer $SUPABASE_JWT" \
  https://api.converio.dev/api/v1/auth/me
```

### POST /auth/company/signup

- `201 CompanyDetailResponse` — company created (or pre-provisioned seat
  linked).
- `400` — JWT is missing the email claim.
- `401` — invalid bearer token.
- `409` — email collision or already onboarded. `X-Error-Code` values:
  `email_in_use_operator`, `email_in_use_recruiter`,
  `already_onboarded`. Plain 409 (no `X-Error-Code`) for duplicate
  company name.
- `422` — body fails schema validation.
- `429` — `Retry-After: 3600`.
- `500` — generic; details in logs.

```bash
curl -X POST -H "Authorization: Bearer $SUPABASE_JWT" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme Robotics","stage":"series_a","hq_location":"SF"}' \
  https://api.converio.dev/api/v1/auth/company/signup
```

### POST /auth/recruiter/signup

- `201 RecruiterResponse` — recruiter created in `pending`.
- `400` — missing email claim.
- `401` — invalid bearer token.
- `409` — email collision or already onboarded. `X-Error-Code` values:
  `email_in_use_operator`, `email_in_use_company`, `already_onboarded`.
- `422` — body fails schema validation.
- `429` — `Retry-After: 3600`.
- `500` — generic.

```bash
curl -X POST -H "Authorization: Bearer $SUPABASE_JWT" \
  -H "Content-Type: application/json" \
  -d '{"full_name":"Jane Doe","domain_expertise":["fintech","ai_infra"],"workspace_type":"agency"}' \
  https://api.converio.dev/api/v1/auth/recruiter/signup
```

### PATCH /companies/{id}/status

- `200 CompanyResponse` — refreshed row with new status.
- `403` — caller is not an active operator.
- `404` — company id not found.
- `422` — transition not in `VALID_STATUS_TRANSITIONS`.
- `500` — generic.

```bash
curl -X PATCH -H "Authorization: Bearer $OPERATOR_JWT" \
  -H "Content-Type: application/json" \
  -d '{"status":"active"}' \
  https://api.converio.dev/api/v1/companies/$COMPANY_ID/status
```

### POST /jobs/intake (gating change)

- `202 JobIntakeAcceptedResponse` — `{ job_id, workflow_id, status: "intake" }`.
- `403 "Not a company user"` — JWT has no seat row.
- `403 "Company not found"` — seat references missing company (race).
- `403 "Company not active"` — company is in `pending_review` / `paused` /
  `churned`.
- `422` — body fails schema or `JobIntakeInput` cross-field validation.
- `429` — `Retry-After: 3600` (10/hour/company).
- `500` — generic; Job row stays in `intake`, REJECT_DUPLICATE keeps a
  retry from spawning two workflows.

---

## 8. Database Changes

### Alembic migration `0004_company_pending_review_status`

Single column-default change:

```python
op.alter_column(
    "companies",
    "status",
    existing_type=sa.String(length=20),
    server_default="pending_review",
    existing_nullable=False,
)
```

Column type and nullability are unchanged. Allowed string values are
constrained at the application layer by `CompanyStatus` (StrEnum) — the
DB column stays a `String(20)` to keep `ALTER TYPE` migrations off the
table.

### CompanyStatus enum (`app/schemas/enums.py`)

```python
class CompanyStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    ACTIVE         = "active"
    PAUSED         = "paused"
    CHURNED        = "churned"
```

Semantics in section 3. `churned` is terminal; the transition table
intentionally has no edges out of it.

### New repository methods

- `CompanyUserRepository.get_by_supabase_user_id(sub)` — seat lookup by
  Supabase `sub`. Backs `/auth/me` step 2 and the
  `already_onboarded` guard.
- `CompanyUserRepository.link_supabase_user_id(user_id, sub)` — Core
  `UPDATE` + `commit` to bind a Supabase identity to a pre-provisioned
  seat. Returns the post-commit row via `get_by_id`.
- `CompanyUserRepository.get_by_email(email)` — pre-existing, now reused
  by the email-backfill branch and the company-side cross-role check.
- `CompanyRepository.update_status(company_id, status)` — Core `UPDATE`
  + `commit`. Raises `404` if the row vanishes between dispatch and
  refetch.
- `RecruiterRepository.get_by_supabase_id(sub)` — recruiter lookup by
  `sub`. Backs `/auth/me` step 3, `get_current_recruiter`, and the
  recruiter `already_onboarded` guard.
- `RecruiterRepository.get_by_email(email)` — used by the company-side
  cross-role uniqueness check.
- `RecruiterRepository.create(...)` — typed signature so endpoint code
  doesn't pass arbitrary kwargs into the ORM model. Owns commit
  semantics.

`OperatorRepository.get_by_email` is also exercised by both signup paths
(operator-takes-precedence cross-role check). It pre-dates this PR.

---

## 9. Out of Scope / Follow-ups

The following are deliberately not in this PR.

- **Frontend.** No sign-up page, OAuth callback handler, role-routed
  shell, company / recruiter onboarding panels, or operator approval
  queue UI ships with this work. The API contract is finalised so
  frontend work can land in parallel.
- **Transactional company + admin-seat creation.** The standard
  self-serve path in `company_signup` (branch 6) inserts the company,
  commits, then inserts the seat and commits. A failure between the two
  leaves an orphan company. There is an explicit `TODO` in the code at
  the seat-insert call site. The right fix is a single transaction
  spanning both inserts (or a dedicated repository method that owns the
  transaction).
- **Operator approval queue UI.** The backend gives the operator console
  everything it needs (`GET /companies` filtered by status,
  `PATCH /companies/{id}/status`, `GET /companies/{id}` with seated
  users). The actual queue view, bulk approval, rejection-reason
  capture, and notification fan-out are the operator-console PR.
- **Audit log table.** Status transitions and seat backfills currently
  go to structured logs only. A persistent audit log table (with
  operator id, target id, before/after state, timestamp, optional
  reason) is a follow-up — the structured log payload is already shaped
  to land directly into such a table.
- **Seat invitation flow.** Today, operator pre-provisions a seat by
  calling `POST /companies/{id}/users` with an email; the user has to
  know to sign in with that email at the right Supabase identity
  provider. A real invitation email (signed link, expiry, audit) is a
  follow-up. The backfill machinery in `/auth/me` already supports it
  — the missing piece is the outbound email + signed token issuance.
- **Distributed rate limiter.** `signup_rate_limiter` is in-process. Any
  multi-worker / multi-pod deployment needs a Redis-backed limiter for
  the limit to be a real ceiling. The interface is already swappable.
- **DB-level cross-role email uniqueness.** Currently enforced at the
  application layer only. A composite uniqueness check (e.g. via a
  shared `auth_identities` table or a function-based unique constraint
  per role table) is a follow-up.
