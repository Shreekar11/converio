"""Execution budget guardrails for agent tool-calling loops.

Each agent run receives a Budget instance. After every tool call, charge() is
called with the consumed resources. The workflow checks should_terminate()
before each iteration; on True it must cleanly exit the loop (synthesize
best-effort result rather than hard-crashing).

Two thresholds per dimension:
- soft: warning logged, loop continues
- hard: should_terminate() returns True, loop must exit

Defaults match the Recruiter Assignment Agent spec (§7 of the agent spec doc).
Other agents may pass different defaults.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.utils.logging import get_logger

LOGGER = get_logger(__name__)


class BudgetExhaustedReason(StrEnum):
    TOOL_CALLS = "tool_calls"
    TOKENS = "tokens"
    WALLCLOCK = "wallclock"
    COST_USD = "cost_usd"


@dataclass
class Budget:
    soft_tool_calls: int = 15
    soft_tokens: int = 30_000
    soft_wallclock_seconds: float = 120.0
    soft_cost_usd: float = 0.50

    hard_tool_calls: int = 25
    hard_tokens: int = 60_000
    hard_wallclock_seconds: float = 300.0
    hard_cost_usd: float = 1.00

    _tool_calls: int = field(default=0, init=False, repr=False)
    _tokens: int = field(default=0, init=False, repr=False)
    _cost_usd: float = field(default=0.0, init=False, repr=False)
    _started_at: float = field(default_factory=time.monotonic, init=False, repr=False)
    _soft_warned: set[BudgetExhaustedReason] = field(
        default_factory=set, init=False, repr=False
    )
    _hard_logged: bool = field(default=False, init=False, repr=False)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def charge(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None:
        self._tool_calls += 1
        self._tokens += tokens
        self._cost_usd += cost_usd

        self._maybe_warn_soft()

    def _maybe_warn_soft(self) -> None:
        checks: list[tuple[BudgetExhaustedReason, bool, str]] = [
            (
                BudgetExhaustedReason.TOOL_CALLS,
                self._tool_calls >= self.soft_tool_calls,
                f"tool_calls={self._tool_calls} soft_limit={self.soft_tool_calls} hard_limit={self.hard_tool_calls}",
            ),
            (
                BudgetExhaustedReason.TOKENS,
                self._tokens >= self.soft_tokens,
                f"tokens={self._tokens} soft_limit={self.soft_tokens} hard_limit={self.hard_tokens}",
            ),
            (
                BudgetExhaustedReason.COST_USD,
                self._cost_usd >= self.soft_cost_usd,
                f"cost_usd={round(self._cost_usd, 4)} soft_limit={self.soft_cost_usd} hard_limit={self.hard_cost_usd}",
            ),
            (
                BudgetExhaustedReason.WALLCLOCK,
                self.elapsed_seconds >= self.soft_wallclock_seconds,
                f"elapsed_seconds={round(self.elapsed_seconds, 1)} soft_limit={self.soft_wallclock_seconds} hard_limit={self.hard_wallclock_seconds}",
            ),
        ]

        for reason, crossed, detail in checks:
            if crossed and reason not in self._soft_warned:
                self._soft_warned.add(reason)
                LOGGER.warning(
                    "agent_budget_soft_limit_crossed dimension=%s %s",
                    reason.value,
                    detail,
                )

    def should_terminate(self) -> bool:
        terminated = self.exhausted_reason() is not None
        if terminated and not self._hard_logged:
            self._hard_logged = True
            reason = self.exhausted_reason()
            LOGGER.warning(
                "agent_budget_hard_limit_hit dimension=%s tool_calls=%d tokens=%d cost_usd=%.4f elapsed_seconds=%.1f",
                reason.value if reason else "unknown",
                self._tool_calls,
                self._tokens,
                self._cost_usd,
                self.elapsed_seconds,
            )
        return terminated

    def is_soft_warning(self) -> bool:
        if self.exhausted_reason() is not None:
            return False
        return (
            self._tool_calls >= self.soft_tool_calls
            or self._tokens >= self.soft_tokens
            or self._cost_usd >= self.soft_cost_usd
            or self.elapsed_seconds >= self.soft_wallclock_seconds
        )

    def exhausted_reason(self) -> BudgetExhaustedReason | None:
        if self._tool_calls >= self.hard_tool_calls:
            return BudgetExhaustedReason.TOOL_CALLS
        if self._tokens >= self.hard_tokens:
            return BudgetExhaustedReason.TOKENS
        if self._cost_usd >= self.hard_cost_usd:
            return BudgetExhaustedReason.COST_USD
        if self.elapsed_seconds >= self.hard_wallclock_seconds:
            return BudgetExhaustedReason.WALLCLOCK
        return None

    def serialize(self) -> dict:
        elapsed = self.elapsed_seconds
        reason = self.exhausted_reason()
        status = "ok" if reason is None else f"hard_limit_hit:{reason.value}"
        return {
            "tool_calls_used": self._tool_calls,
            "tool_calls_remaining": max(0, self.hard_tool_calls - self._tool_calls),
            "tokens_used": self._tokens,
            "tokens_remaining": max(0, self.hard_tokens - self._tokens),
            "cost_usd_used": round(self._cost_usd, 4),
            "cost_usd_remaining": round(max(0.0, self.hard_cost_usd - self._cost_usd), 4),
            "elapsed_seconds": round(elapsed, 1),
            "wallclock_remaining": round(
                max(0.0, self.hard_wallclock_seconds - elapsed), 1
            ),
            "status": status,
        }
