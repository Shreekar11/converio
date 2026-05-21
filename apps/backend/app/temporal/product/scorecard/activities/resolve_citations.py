"""scorecard.resolve_citations — three-tier citation resolution.

Resolves every `ScorecardDimension.citation.text` into a precise
location within the candidate profile, producing char offsets for the
operator UI to highlight. The frontend uses the offsets to render
direct evidence highlights and the `resolution_method` field to label
soft/placeholder citations.

Tiering (plan §13)
------------------
* **Tier 1 — direct fuzzy text match (rapidfuzz, threshold 0.85).**
  We slide a window across `candidate_profile_text` in 200-char chunks
  with 100-char overlap, compute `fuzz.partial_ratio` against the
  citation text, and pick the chunk with the highest score. If
  `rapidfuzz` is not installed (e.g. on a fresh dev machine before
  `pip install`), we fall back to `difflib.SequenceMatcher.ratio()`
  with a matching contract — same threshold semantics, slightly worse
  matching quality. This avoids hard-failing the whole resolver over a
  missing optional dependency.
* **Tier 2 — pgvector semantic chunk search (threshold 0.65).**
  Currently a graceful no-op: the project does NOT have a
  `candidate_profile_chunks` table or any chunked embeddings store.
  This stage is wired so v2 can drop the table in and flip the
  `_has_chunks_table` probe to `True` without changing the activity
  signature. Until then we log and skip — Tier 3 picks up the slack.
* **Tier 3 — placeholder fallback.** Always succeeds. Records the
  citation with `resolution_method="placeholder"` and no char offsets.

Determinism
-----------
Tier 1 is deterministic on identical inputs (chunking is index-driven,
not RNG-driven; rapidfuzz/difflib scores are pure functions of input
text). Tier 2 will be deterministic when wired — pgvector cosine
similarity on a fixed embedding set + a fixed query is reproducible.
Tier 3 is trivially deterministic.

This matters because Temporal replays this activity from event-history
inputs; non-deterministic resolution would corrupt replay and yield
different citations on workflow re-runs.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.schemas.product.scorecard import Citation, ScorecardDimension
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Tier-1 fuzzy threshold (0.85). Calibrated on the Insura AI dataset
# (plan §13). Lower values produce false positives — substring matches
# that "look close" but point to unrelated profile text. Higher values
# push too much traffic to Tier 2/3.
TIER1_THRESHOLD: float = 0.85

# Tier-2 semantic threshold (0.65). Used only when pgvector chunks are
# available; calibrated against the same dataset.
TIER2_THRESHOLD: float = 0.65

# Sliding-window parameters for Tier 1. 200-char chunks balance recall
# (long enough to contain a multi-clause sentence) against precision
# (short enough to localize the highlight). 100-char overlap ensures a
# citation that straddles two chunks still scores well in at least one.
_CHUNK_SIZE: int = 200
_CHUNK_STRIDE: int = 100

# rapidfuzz returns scores in [0, 100]; we normalize to [0, 1] when
# comparing against TIER1_THRESHOLD. Keeping the constant in [0, 1]
# keeps the contract identical across the rapidfuzz / difflib backends.
_RAPIDFUZZ_NORMALIZER: float = 100.0

# Cap on candidate-profile-text size for chunking. Very long resumes
# (>50k chars after concat with GitHub signals) blow chunk count past
# 500 and inflate activity wallclock. Anything beyond this cap is
# truncated; the citation resolver still runs but only matches against
# the leading window. This is a soft cap — operators rarely exceed it.
_MAX_PROFILE_TEXT_CHARS: int = 60_000


class ResolveCitationsInput(BaseModel):
    """Input contract.

    `candidate_id` is included so Tier 2 (pgvector lookup) can filter
    chunks by candidate. Phase 3 does not use it yet, but Phase 5+ will
    — keeping it in the contract avoids a breaking change later.
    """

    model_config = ConfigDict(extra="forbid")

    dimensions: list[dict[str, Any]] = Field(
        ..., description="ScorecardDimension dicts (with `citation` sub-dict)."
    )
    candidate_profile_text: str = Field(
        ...,
        description="Full candidate profile text (resume + GitHub signals + notes).",
    )
    candidate_id: str = Field(
        ..., description="Candidate UUID as string — used by Tier 2 pgvector lookup."
    )


class ResolveCitationsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimensions: list[dict[str, Any]] = Field(
        ..., description="Same dim dicts with citation fields updated."
    )


# ────────────────────────────────────────────────────────────────────────
# Tier 1 — fuzzy text match
# ────────────────────────────────────────────────────────────────────────


def _try_import_rapidfuzz() -> Any | None:
    """Return the rapidfuzz.fuzz module, or None if not installed.

    We do this lazily (rather than at module import) so the resolver
    activity loads cleanly on machines without the optional dep — the
    `python -c "from ... import compute_overall_match_score"` smoke
    test in the Phase 3 acceptance criteria MUST pass even if rapidfuzz
    is absent, and importing this activity package transitively imports
    this module.
    """
    try:
        from rapidfuzz import fuzz  # type: ignore

        return fuzz
    except ImportError:
        return None


def _chunk_indices(text_len: int) -> list[tuple[int, int]]:
    """Generate (start, end) windows across a text of length `text_len`.

    Deterministic and total: covers the entire text with overlapping
    windows. The final window is always anchored to `text_len` so the
    tail of the text is never silently dropped.
    """
    if text_len <= 0:
        return []
    if text_len <= _CHUNK_SIZE:
        return [(0, text_len)]
    starts = list(range(0, text_len - _CHUNK_SIZE + 1, _CHUNK_STRIDE))
    # Anchor the last window so we cover the tail even when stride doesn't
    # divide cleanly. Duplicates are filtered below.
    if starts[-1] + _CHUNK_SIZE < text_len:
        starts.append(text_len - _CHUNK_SIZE)
    seen: set[int] = set()
    out: list[tuple[int, int]] = []
    for s in starts:
        if s in seen:
            continue
        seen.add(s)
        out.append((s, min(s + _CHUNK_SIZE, text_len)))
    return out


def _tier1_match(
    citation_text: str,
    profile_text: str,
    fuzz_module: Any | None,
) -> tuple[float, int, int] | None:
    """Try Tier 1 fuzzy match. Return (score, start, end) or None.

    Score is normalized to [0, 1]. We return None either when there's
    no profile text to match against or when the best window scores
    below `TIER1_THRESHOLD`.
    """
    if not citation_text or not profile_text:
        return None

    windows = _chunk_indices(len(profile_text))
    if not windows:
        return None

    best_score = 0.0
    best_window: tuple[int, int] | None = None
    citation_lower = citation_text.lower()

    if fuzz_module is not None:
        # rapidfuzz path — fast C implementation. partial_ratio finds
        # the best-aligned substring in the chunk that matches the
        # citation; ratio()/WRatio() would over-penalize length mismatch.
        for start, end in windows:
            chunk_lower = profile_text[start:end].lower()
            score = fuzz_module.partial_ratio(citation_lower, chunk_lower)
            normalized = score / _RAPIDFUZZ_NORMALIZER
            if normalized > best_score:
                best_score = normalized
                best_window = (start, end)
    else:
        # difflib fallback — slower (pure Python) but always available.
        # SequenceMatcher.ratio() is symmetric and bounded in [0, 1],
        # so the threshold semantics match rapidfuzz partial_ratio.
        # Quality is materially worse on long chunks; we accept that
        # because the alternative is a hard dependency failure.
        from difflib import SequenceMatcher

        for start, end in windows:
            chunk_lower = profile_text[start:end].lower()
            score = SequenceMatcher(None, citation_lower, chunk_lower).ratio()
            if score > best_score:
                best_score = score
                best_window = (start, end)

    if best_window is None or best_score < TIER1_THRESHOLD:
        return None

    return (best_score, best_window[0], best_window[1])


# ────────────────────────────────────────────────────────────────────────
# Tier 2 — pgvector semantic chunk search (graceful no-op in v1)
# ────────────────────────────────────────────────────────────────────────


async def _has_chunks_table() -> bool:
    """Detect whether a candidate-profile-chunks table is wired.

    Returns False today (v1 ships without chunked embeddings — only
    candidate-level `Candidate.embedding` exists, which is too coarse
    for citation char offsets). Kept as a real probe (rather than a
    `return False`) so that v2 can drop a table and have this resolver
    activate without any code change here.
    """
    try:
        from app.database import models  # noqa: F401

        # Probe for any of the plausible chunk-table model names. None
        # of these exist today; this check is forward-compatible.
        for candidate_name in (
            "CandidateProfileChunk",
            "CandidateProfileChunks",
            "ProfileChunk",
        ):
            if hasattr(models, candidate_name):
                return True
    except ImportError:  # pragma: no cover — models.py is required
        return False
    return False


async def _tier2_match(
    citation_text: str,
    candidate_id: str,
) -> tuple[float, int, int] | None:
    """Try Tier 2 semantic match via pgvector. Returns None in v1.

    Hook signature is finalized so the v2 implementation slots in
    without changing the calling code in `resolve_citations`.
    """
    if not await _has_chunks_table():
        return None

    # v2 implementation outline (NOT executed in v1):
    #   1. embed citation_text via app.core.embeddings.embed_text
    #   2. SELECT chunk_text, char_offset_start, char_offset_end,
    #      1 - (embedding <=> :query_vec) AS score
    #      FROM candidate_profile_chunks
    #      WHERE candidate_id = :cid
    #      ORDER BY score DESC
    #      LIMIT 1
    #   3. if score >= TIER2_THRESHOLD: return (score, start, end)
    LOGGER.info(
        "Tier 2 pgvector path reached but not implemented in v1",
        extra={"candidate_id": candidate_id},
    )
    return None


# ────────────────────────────────────────────────────────────────────────
# Per-dim resolver — orchestrates the tiers in order
# ────────────────────────────────────────────────────────────────────────


async def _resolve_one_citation(
    dim: ScorecardDimension,
    profile_text: str,
    candidate_id: str,
    fuzz_module: Any | None,
) -> Citation:
    """Resolve a single dimension's citation through the tier ladder.

    Returns a fresh `Citation` (never mutates the input) so the function
    is idempotent on retry. If the dim has no citation, returns a
    placeholder Citation with `text=""` — the caller writes this back
    onto the dim dict so the downstream schema stays consistent.
    """
    original = dim.citation
    if original is None:
        return Citation(
            text="",
            resolution_method="placeholder",
            confidence=None,
        )

    citation_text = original.text
    if not citation_text or not citation_text.strip():
        return Citation(
            text=citation_text,
            resolution_method="placeholder",
            confidence=None,
        )

    # Tier 1
    tier1 = _tier1_match(citation_text, profile_text, fuzz_module)
    if tier1 is not None:
        score, start, end = tier1
        return Citation(
            text=citation_text,
            char_offset_start=start,
            char_offset_end=end,
            resolution_method="direct_text_match",
            confidence=round(score, 4),
        )

    # Tier 2
    tier2 = await _tier2_match(citation_text, candidate_id)
    if tier2 is not None:
        score, start, end = tier2
        return Citation(
            text=citation_text,
            char_offset_start=start,
            char_offset_end=end,
            resolution_method="pgvector_semantic",
            confidence=round(score, 4),
        )

    # Tier 3 — always succeeds. Keep the LLM-provided text so the UI
    # can still render the quote, even without a verifiable offset.
    return Citation(
        text=citation_text,
        resolution_method="placeholder",
        confidence=None,
    )


@ActivityRegistry.register("scorecard", "resolve_citations")
@activity.defn(name="scorecard.resolve_citations")
async def resolve_citations(payload: dict) -> dict:
    """Resolve citations for every dim, in order, with tiered fallback.

    Args:
        payload: dict matching `ResolveCitationsInput`.

    Returns:
        dict matching `ResolveCitationsOutput`. The dim dicts have
        their `citation` sub-dicts updated; all other dim fields are
        preserved unchanged.

    Raises:
        pydantic.ValidationError: on malformed input.
    """
    model = ResolveCitationsInput.model_validate(payload)

    profile_text = (model.candidate_profile_text or "")[:_MAX_PROFILE_TEXT_CHARS]
    fuzz_module = _try_import_rapidfuzz()
    if fuzz_module is None:
        LOGGER.info(
            "resolve_citations: rapidfuzz not available — using difflib fallback",
        )

    parsed_dims: list[ScorecardDimension] = [
        ScorecardDimension.model_validate(d) for d in model.dimensions
    ]

    resolution_counts = {
        "direct_text_match": 0,
        "pgvector_semantic": 0,
        "placeholder": 0,
    }
    updated_dim_dicts: list[dict[str, Any]] = []
    for dim in parsed_dims:
        resolved_citation = await _resolve_one_citation(
            dim, profile_text, model.candidate_id, fuzz_module
        )
        resolution_counts[resolved_citation.resolution_method] += 1
        # Rebuild the dim with the new citation. `model_copy(update=...)`
        # gives us a fresh ScorecardDimension that still re-validates,
        # so any drift between resolution and dim schema raises here
        # rather than at the persistence boundary.
        new_dim = dim.model_copy(update={"citation": resolved_citation})
        updated_dim_dicts.append(new_dim.model_dump(mode="json"))

    LOGGER.info(
        "Resolved citations",
        extra={
            "dim_count": len(parsed_dims),
            "direct_text_match": resolution_counts["direct_text_match"],
            "pgvector_semantic": resolution_counts["pgvector_semantic"],
            "placeholder": resolution_counts["placeholder"],
            "rapidfuzz_available": fuzz_module is not None,
            "profile_text_chars": len(profile_text),
        },
    )

    return ResolveCitationsOutput(dimensions=updated_dim_dicts).model_dump(mode="json")
