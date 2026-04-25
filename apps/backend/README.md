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
