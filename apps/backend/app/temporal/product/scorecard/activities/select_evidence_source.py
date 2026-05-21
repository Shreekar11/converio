"""scorecard.select_evidence_source — LLM picks the next fetcher.

For each low-confidence dimension, the self-correction loop asks the
LLM "which evidence source should I consult to raise this dim's
confidence?". The answer is a `tool_name` from the registry (one of the
5 MVP fetchers) plus an optional keyword filter (for `fetch_repo_readmes`).

This is the *cheap* LLM call in the reflective loop: it runs on every
low-conf dim, so we use Gemini 2.5 Flash (cheaper, faster) rather than
Pro. The Pro tier is reserved for the rescoring step where token-by-
token quality directly drives the persisted scorecard.

Determinism & trust
-------------------
* **`tool_name="skip"` is a valid output.** If the candidate has no
  GitHub username, or if the dimension has no GitHub proxy (e.g.
  `communication_clarity` in v1), the LLM is instructed to return
  `skip` and the workflow flags the dim as `evidence_limited`. This
  avoids burning budget on hopeless fetches.
* **Hard short-circuit when `github_username is None`.** The LLM is
  unreliable about respecting "you have no tool"; we enforce it in
  Python so a missing GitHub never produces a tool call.
* **Tool-name validation.** The LLM is constrained by the
  `available_tools` list in the prompt, but we still validate the
  returned `tool_name` against that list. An unknown tool is downgraded
  to `skip` with a logged warning rather than raising — the workflow
  needs *some* answer per iteration to keep progressing.
* **Trust boundary.** The dimension data passed in `low_conf_dim` is
  itself LLM-authored (it came from `score_candidate_dimensions`). We
  treat it as untrusted user content: delimited inside `<<<…>>>`
  fences, never interpolated into the system role.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field
from temporalio import activity

from app.core.config import settings
from app.core.llm import LLMMessage, get_llm_client
from app.schemas.product.scorecard import EvidenceSourceChoice
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Flash-tier model for the cheap planner call. Falls back to the
# operator-configured Gemini default if no flash variant is configured.
_DEFAULT_FLASH_MODEL = "gemini-2.5-flash"

_SKIP_TOOL_NAME = "skip"

# Token / cost estimate knobs (see `score_candidate_dimensions` for
# rationale). The Flash tier is ~10x cheaper than Pro per token; we
# bake that into the cost factor here.
_COST_PER_TOKEN_USD = 0.0000001
_TOKEN_ESTIMATE_MULTIPLIER = 1.3


# Mapping table embedded in the system prompt. Mirrors §9.2 (MVP
# GitHub-only collapse) verbatim so the LLM has a deterministic guide
# rather than reinventing the mapping each call.
_DIMENSION_MAPPING_GUIDE = (
    "DIMENSION → TOOL MAPPING (MVP, GitHub-only):\n"
    "- distributed_systems_depth → fetch_repo_readmes with keyword_filter=["
    '"kafka","redis","grpc","kubernetes","etcd","zookeeper","kinesis"]\n'
    "- system_design_thinking → fetch_repo_readmes looking for repos with "
    "ARCHITECTURE.md, DESIGN.md, or docs/ directories "
    '(keyword_filter=["architecture","design","docs"])\n'
    "- language_depth → fetch_repo_languages + fetch_commit_history\n"
    "- open_source_signals → fetch_pr_review_history + fetch_user_orgs_and_stars\n"
    "- consistency_over_time → fetch_commit_history (24-month aggregate)\n"
    "- Any other dimension with no GitHub proxy (e.g. communication_clarity, "
    "ml_research_depth without GitHub presence) → return tool_name=\"skip\"."
)


_SYSTEM_PROMPT_BASE = (
    "You are the evidence-routing planner for a candidate-scoring agent.\n"
    "Given a single low-confidence rubric dimension and the candidate's profile "
    "summary, pick ONE tool from the available tools that will most likely raise "
    "the dimension's confidence.\n"
    "\n"
    "RULES:\n"
    '- Output ONLY JSON matching the EvidenceSourceChoice schema.\n'
    '- "tool_name" MUST be exactly one of the names in <<<AVAILABLE_TOOLS>>>, '
    f'OR the literal string "{_SKIP_TOOL_NAME}" if no tool applies.\n'
    '- "reasoning" is 1-2 sentences explaining the choice.\n'
    '- "keyword_filter" is only meaningful for fetch_repo_readmes; null for '
    "other tools.\n"
    "- If the candidate has no GitHub presence, return tool_name=\"skip\".\n"
    "- Do not invent tools; do not call multiple tools.\n"
    "\n"
    f"{_DIMENSION_MAPPING_GUIDE}"
)


def _resolve_flash_model() -> str:
    """Resolve the Gemini Flash model name.

    Preference order: an operator-configured `gemini_model` whose name
    contains "flash" (the cheap config), else the default Flash constant.
    Unlike `score_candidate_dimensions` we DO accept any flash variant
    the operator configured — running this call on a slightly different
    Flash version is acceptable, since the only output is a tool name.
    """
    configured = (settings.llm.gemini_model or "").strip()
    if configured and "flash" in configured.lower():
        return configured
    return _DEFAULT_FLASH_MODEL


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text.split()) * _TOKEN_ESTIMATE_MULTIPLIER)


class SelectEvidenceSourceInput(BaseModel):
    """Activity input.

    `available_tools` is the workflow-side allowlist of tool names the
    LLM may choose from. The workflow narrows this list at each step
    (e.g. drops a tool that has already been called for this dim) so the
    planner doesn't loop.
    """

    model_config = ConfigDict(extra="forbid")

    low_conf_dim: dict = Field(..., description="`LowConfDim`.model_dump() shape.")
    candidate_profile_summary: str = Field(..., min_length=0, max_length=4000)
    available_tools: list[str] = Field(..., min_length=1)
    github_username: str | None = Field(default=None)


class SelectEvidenceSourceOutput(BaseModel):
    """Activity output.

    `tool_name` is either a member of `available_tools` or the literal
    `"skip"`. `keyword_filter` is None for non-keyword tools (it is
    emitted but always None for tools other than `fetch_repo_readmes`).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., min_length=1)
    reasoning: str = Field(..., min_length=1)
    keyword_filter: list[str] | None = None
    tokens_used: int = Field(..., ge=0)
    cost_usd: float = Field(..., ge=0.0)


def _build_user_prompt(
    *,
    low_conf_dim: dict,
    candidate_profile_summary: str,
    available_tools: list[str],
    github_username: str | None,
) -> str:
    """Delimited user-role prompt.

    All non-system content is fenced; the LLM is instructed to ignore
    instructions inside the fences (the system role already states the
    output schema).
    """
    return (
        "<<<LOW_CONFIDENCE_DIMENSION>>>\n"
        f"{json.dumps(low_conf_dim, sort_keys=True, default=str)}\n"
        "<<<END_LOW_CONFIDENCE_DIMENSION>>>\n"
        "\n"
        "<<<CANDIDATE_PROFILE_SUMMARY>>>\n"
        f"{candidate_profile_summary or '(no summary provided)'}\n"
        "<<<END_CANDIDATE_PROFILE_SUMMARY>>>\n"
        "\n"
        "<<<GITHUB_USERNAME>>>\n"
        f"{github_username if github_username else '(none — return skip)'}\n"
        "<<<END_GITHUB_USERNAME>>>\n"
        "\n"
        "<<<AVAILABLE_TOOLS>>>\n"
        f"{json.dumps(sorted(available_tools))}\n"
        "<<<END_AVAILABLE_TOOLS>>>\n"
        "\n"
        "Pick the single best tool, or skip."
    )


def _skip_result(reason: str, tokens_used: int = 0) -> SelectEvidenceSourceOutput:
    """Build a deterministic skip result. Used by the no-GitHub short-circuit
    and the unknown-tool downgrade path."""
    return SelectEvidenceSourceOutput(
        tool_name=_SKIP_TOOL_NAME,
        reasoning=reason,
        keyword_filter=None,
        tokens_used=tokens_used,
        cost_usd=round(tokens_used * _COST_PER_TOKEN_USD, 10),
    )


@ActivityRegistry.register("scorecard", "select_evidence_source")
@activity.defn(name="scorecard.select_evidence_source")
async def select_evidence_source(payload: dict) -> dict:
    """Pick the next fetcher tool for a low-confidence dimension.

    Args:
        payload: dict matching `SelectEvidenceSourceInput`.

    Returns:
        `SelectEvidenceSourceOutput.model_dump(mode="json")`.

    Behavior:
        * If `github_username is None` → returns `tool_name="skip"` without
          calling the LLM.
        * If the LLM returns a `tool_name` not in `available_tools` and
          not `"skip"` → downgraded to `"skip"` with a warning log.
        * On LLM transport / validation errors → propagates so Temporal
          retries the activity.
    """
    input_model = SelectEvidenceSourceInput.model_validate(payload)

    # ---- Short-circuit: no GitHub presence → skip ------------------------
    # The LLM is told this rule in the prompt too, but we enforce it here
    # so a misbehaving model cannot produce a tool call that the workflow
    # then has to dispatch with a guaranteed-empty result.
    if input_model.github_username is None or not input_model.github_username.strip():
        LOGGER.info(
            "scorecard.select_evidence_source: no GitHub username → skip",
            extra={"dim": input_model.low_conf_dim.get("name")},
        )
        return _skip_result(
            "Candidate has no GitHub username; no MVP fetcher applies."
        ).model_dump(mode="json")

    llm = get_llm_client()
    model_name = _resolve_flash_model()

    user_prompt = _build_user_prompt(
        low_conf_dim=input_model.low_conf_dim,
        candidate_profile_summary=input_model.candidate_profile_summary,
        available_tools=input_model.available_tools,
        github_username=input_model.github_username,
    )
    messages = [
        LLMMessage(role="system", content=_SYSTEM_PROMPT_BASE),
        LLMMessage(role="user", content=user_prompt),
    ]

    LOGGER.info(
        "scorecard.select_evidence_source: deciding",
        extra={
            "dim": input_model.low_conf_dim.get("name"),
            "model": model_name,
            "available_tools": sorted(input_model.available_tools),
        },
    )

    # Intentionally NOT wrapping the LLM call in try/except — transport /
    # validation errors propagate so Temporal's activity retry policy
    # fires. The planner has no useful local fallback at the LLM layer.
    choice: EvidenceSourceChoice = await llm.structured_complete(
        messages=messages,
        schema=EvidenceSourceChoice,
        model=model_name,
    )

    # ---- Token / cost estimate -------------------------------------------
    response_serialized = choice.model_dump_json()
    tokens_used = _estimate_tokens(user_prompt) + _estimate_tokens(response_serialized)
    cost_usd = round(tokens_used * _COST_PER_TOKEN_USD, 10)

    LOGGER.warning(
        "Token usage is estimated, not provider-reported",
        extra={"tokens_used_estimate": tokens_used, "cost_usd": cost_usd},
    )

    # ---- Validate the chosen tool ----------------------------------------
    tool_name = choice.tool_name.strip()
    allowed = set(input_model.available_tools) | {_SKIP_TOOL_NAME}
    if tool_name not in allowed:
        LOGGER.warning(
            "scorecard.select_evidence_source: unknown tool → downgrading to skip",
            extra={
                "returned_tool": tool_name,
                "available_tools": sorted(input_model.available_tools),
                "dim": input_model.low_conf_dim.get("name"),
            },
        )
        downgrade = _skip_result(
            f"LLM returned unknown tool {tool_name!r}; downgraded to skip.",
            tokens_used=tokens_used,
        )
        return downgrade.model_dump(mode="json")

    # keyword_filter only makes sense for fetch_repo_readmes; null it
    # out for other tools so the dispatch site doesn't have to guard.
    keyword_filter = choice.keyword_filter
    if tool_name != "fetch_repo_readmes":
        keyword_filter = None

    output = SelectEvidenceSourceOutput(
        tool_name=tool_name,
        reasoning=choice.reasoning,
        keyword_filter=keyword_filter,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
    )

    LOGGER.info(
        "scorecard.select_evidence_source: decision",
        extra={
            "dim": input_model.low_conf_dim.get("name"),
            "tool_name": tool_name,
            "keyword_filter": keyword_filter,
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
        },
    )
    return output.model_dump(mode="json")
