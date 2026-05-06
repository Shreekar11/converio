"""LLM decision activity for agent tool-calling loops.

`core.llm_decide_next_tool` is a Temporal activity that wraps a single LLM
call: given the agent goal, available tools, call history, and remaining
budget, it returns the next tool invocation the LLM wants to make.

The LLM uses `structured_complete` (JSON-mode) with a `ToolCallDecision`
schema so the output is always a valid, typed tool call -- never freeform text.
Parse failures bubble up as exceptions so Temporal's retry policy handles them.

Per CLAUDE.md AI/LLM rules:
- System prompt contains only trusted content (goal, tool registry, format instructions).
- User-controlled fields (classification, intake notes) flow through user role
  with explicit delimiters.
- LLM output is never passed to eval/shell/innerHTML.
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from temporalio import activity

from app.core.llm import LLMMessage, get_llm_client
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Placeholder cost factor: $1e-6 per estimated token. Real cost accounting
# (per-provider, per-model pricing tables) is future work tracked separately.
_COST_PER_TOKEN_USD = 0.000001

# Rough word-to-token multiplier used because LLMResponse does not currently
# expose provider token counts. Replace once the LLM client surfaces usage.
_TOKEN_ESTIMATE_MULTIPLIER = 1.3


class ToolCallDecision(BaseModel):
    """Structured output schema enforced on the LLM via JSON mode."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(..., description="Exact tool name from the registry")
    args: dict[str, Any] = Field(..., description="Arguments for the tool")
    reasoning: str = Field(
        ...,
        max_length=500,
        description="1-2 sentence rationale for this choice",
    )

    @field_validator("tool_name")
    @classmethod
    def _tool_name_non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("tool_name must be a non-empty string")
        return value.strip()


class ToolCallResult(BaseModel):
    """Activity output: decision plus telemetry for the planning loop."""

    tool_name: str
    args: dict[str, Any]
    reasoning: str
    tokens_used: int
    cost_usd: float
    latency_ms: int


def _build_system_prompt(goal_text: str, tool_catalog_md: str) -> str:
    """Trusted system prompt. Only goal + tool catalog (operator-authored) inlined."""
    return (
        "You are the planning agent for a recruiter assignment system.\n"
        "\n"
        "Your goal:\n"
        f"{goal_text}\n"
        "\n"
        "You have access to these tools:\n"
        f"{tool_catalog_md}\n"
        "\n"
        "INSTRUCTIONS:\n"
        "- Respond with ONLY a JSON object matching the ToolCallDecision schema.\n"
        "- Choose ONE tool per response.\n"
        '- "tool_name" must exactly match a tool name from the list above.\n'
        '- "args" must match the tool\'s argument schema exactly.\n'
        '- "reasoning" explains in 1-2 sentences WHY you chose this tool at this step.\n'
        '- If you are satisfied with the recruiter pool and ready to propose, '
        'call "propose_assignment_set".\n'
        "- Never call a terminal tool until you have scored and ranked candidates."
    )


def _build_user_prompt(history_md: str, budget_json: str, context_json: str) -> str:
    """Delimited user-role payload. Per CLAUDE.md: untrusted/dynamic content
    stays out of the system role.
    """
    return (
        "<<<CALL_HISTORY>>>\n"
        f"{history_md}\n"
        "<<<END_CALL_HISTORY>>>\n"
        "\n"
        "<<<BUDGET_REMAINING>>>\n"
        f"{budget_json}\n"
        "<<<END_BUDGET_REMAINING>>>\n"
        "\n"
        "<<<CONTEXT>>>\n"
        f"{context_json}\n"
        "<<<END_CONTEXT>>>\n"
        "\n"
        "What is your next tool call?"
    )


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(
            f"llm_decide_next_tool: '{key}' is required and must be a string"
        )
    return value


def _estimate_tokens(content: str) -> int:
    """Rough token estimate from word count.

    LLMResponse does not currently expose provider token counts; this is a
    placeholder so cost/latency telemetry has a number to report. A WARNING is
    logged once per call so the estimate is never silently trusted.
    """
    words = len(content.split())
    return int(words * _TOKEN_ESTIMATE_MULTIPLIER)


@ActivityRegistry.register("core", "llm_decide_next_tool")
@activity.defn(name="core.llm_decide_next_tool")
async def llm_decide_next_tool(payload: dict) -> dict:
    """Single LLM call returning the next tool invocation.

    Input payload keys:
        goal_text: str               -- full goal string for this agent
        tool_catalog_md: str         -- rendered tool registry markdown
        history_md: str              -- rendered call history
        budget_json: str             -- JSON string of budget.serialize()
        context_json: str            -- JSON string of role context (classification etc.)

    Returns:
        ToolCallResult serialized as dict.

    Raises:
        ValueError: if payload is malformed or LLM returns a tool_name not in
            the tool catalog.
        Any LLM exception bubbles up so Temporal's `_LLM_RETRY` policy fires.
    """
    if not isinstance(payload, dict):
        raise ValueError("llm_decide_next_tool: payload must be a dict")

    goal_text = _require_str(payload, "goal_text")
    tool_catalog_md = _require_str(payload, "tool_catalog_md")
    history_md = _require_str(payload, "history_md")
    budget_json = _require_str(payload, "budget_json")
    context_json = _require_str(payload, "context_json")

    if not goal_text.strip():
        raise ValueError("llm_decide_next_tool: 'goal_text' must be non-empty")
    if not tool_catalog_md.strip():
        raise ValueError("llm_decide_next_tool: 'tool_catalog_md' must be non-empty")

    # Crude step counter for observability; "## Step" / line count would be more
    # accurate but the caller controls history rendering, so newline count is
    # a stable enough proxy.
    step = history_md.count("\n### ") if history_md.strip() else 0

    LOGGER.info(
        "LLM deciding next tool",
        extra={
            "step": step,
            "budget_remaining": budget_json,
            "history_len": len(history_md),
            "context_len": len(context_json),
        },
    )

    messages = [
        LLMMessage(
            role="system",
            content=_build_system_prompt(goal_text, tool_catalog_md),
        ),
        LLMMessage(
            role="user",
            content=_build_user_prompt(history_md, budget_json, context_json),
        ),
    ]

    llm = get_llm_client()

    started = time.monotonic()
    # Intentionally NOT wrapping in try/except: LLM exceptions must propagate
    # so Temporal's retry policy at the workflow call site can handle them.
    decision: ToolCallDecision = await llm.structured_complete(
        messages=messages,
        schema=ToolCallDecision,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    # Validate the chosen tool actually exists in the catalog. The catalog is
    # rendered as markdown with each tool as `### {tool_name}`; a simple
    # substring check is sufficient because tool names are operator-controlled
    # identifiers (no regex metacharacters expected).
    expected_header = f"### {decision.tool_name}"
    if expected_header not in tool_catalog_md:
        LOGGER.error(
            "LLM returned unknown tool",
            extra={
                "tool_name": decision.tool_name,
                "reasoning": decision.reasoning,
            },
        )
        raise ValueError(
            f"LLM returned unknown tool: {decision.tool_name!r}"
        )

    # Token / cost telemetry. The current LLMResponse model has no usage field,
    # so we estimate from the serialized decision text. Logged as a warning so
    # this estimate is never silently treated as authoritative.
    estimated_payload = (
        f"{decision.tool_name} {decision.reasoning} {decision.args!r}"
    )
    tokens_used = _estimate_tokens(estimated_payload)
    cost_usd = round(tokens_used * _COST_PER_TOKEN_USD, 8)
    LOGGER.warning(
        "Token usage is estimated, not provider-reported",
        extra={"tokens_used_estimate": tokens_used},
    )

    LOGGER.info(
        "LLM decision",
        extra={
            "tool_name": decision.tool_name,
            "reasoning_len": len(decision.reasoning),
            "latency_ms": latency_ms,
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
        },
    )

    result = ToolCallResult(
        tool_name=decision.tool_name,
        args=decision.args,
        reasoning=decision.reasoning,
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    return result.model_dump(mode="json")
