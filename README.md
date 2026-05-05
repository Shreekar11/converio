# Converio

Converio is an AI-native talent matching engine built as a proof-of-work for a managed recruiting workflow.

## What the system does

Converio mirrors a recruiter-centric hiring model with three actors:
- companies submit managed job intake,
- recruiters submit candidates for assigned roles,
- operators review high-impact decisions in the loop.

The platform solves two matching problems:
- **Recruiter ↔ Role matching** (assign the best recruiter for a role),
- **Candidate ↔ Role matching** (score and rank candidates against a rubric).

## System overview

The architecture combines durable orchestration with AI enrichment:
- **Temporal** workflows orchestrate long-running, retry-safe pipelines.
- **FastAPI backend** provides API triggers, auth, and workflow control.
- **Postgres + pgvector** store structured entities and embeddings.
- **Neo4j** stores graph relationships across candidates, recruiters, and role context.
- **Human-in-the-loop signals** gate critical decisions (operator assignment approval and company shortlist review).

## Multi-agent flow (Temporal)

Converio follows a hybrid orchestration model where Temporal coordinates deterministic workflows and autonomous agents across three tiers (T1 deterministic workflows, T2 LLM-augmented workflows, T3 autonomous agents).

End-to-end flow:
- **Agent 1: Job Intake (T1 root workflow)** starts on company intake, builds the role classification and rubric artifact.
- **Agent 7: Planner (T3 long-lived child workflow)** is spawned by Job Intake and owns the job lifecycle: emit initial plan, dispatch workers, consume signals, and replan when conditions change.
- **Agent 0: Recruiter Assignment (T2)** is dispatched by Planner; it runs adaptive evidence gathering, then pauses for **operator approval** via Temporal Signal.
- **Agent 2: Candidate Indexing (T2)** runs per resume and signals Planner when candidates are indexed.
- **Agent 4: Scorecard (T3)** is typically spawned per indexed candidate (parallel, plan-governed concurrency).
- **Agent 5: Ranking (T1)** runs in batches (for example when enough scorecards are ready) and updates shortlist output.
- **Agent 6: Ambient Monitor (T1 scheduled workflow)** checks pool health and signals Planner to trigger fallback actions.
- **Agent 3: Sourcing (T3, conditional)** is launched by Planner when pool quality/volume is weak.
- **Company review (HITL)** feeds back through signals; Planner can re-run scorecard/ranking with updated constraints.

Temporal primitives (child workflows, activities, signals, schedules, retries, replay) make the pipeline durable and auditable for long-running hiring loops with human gates.