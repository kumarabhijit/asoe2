"""ADR-038 §8.1 — Per-tier budget enforcement for the L4 harness.

The harness enforces budgets so the Case Agent operates as if budget
were infinite (Boris harness rule #5). On exhaustion, the harness
preempts the loop and routes to ``MANUAL_REVIEW_REQUIRED``.

Budgets are dimensions:
  * input_tokens — per-inference input (charged at full input rate)
  * output_tokens — per-inference output
  * iterations — total agent loop turns
  * wall_clock_ms — total wall-clock for the case run
  * cost_usd — total dollar spend

Per ADR-038 §8.1:
  T1: 4k/1k input/output, 1 LLM call, <500ms, <$0.001
  T2: 16k/4k, 6 iterations, <8s, <$0.05
  T3: 8k/2k post-compaction + retrieval, 8 iterations, <12s, <$0.08
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from contracts.models import OrderCase


@dataclass
class CaseBudget:
    """Per-event budget. Created on each agent run for a case.

    The harness `deduct(...)` call is the SINGLE place the budget
    counters move; tools read but don't write.
    """

    tier: int

    # Limits (set per-tier in `for_tier`).
    max_input_tokens: int
    max_output_tokens: int
    max_iterations: int
    max_wall_clock_ms: int
    max_cost_usd: float

    # Counters.
    input_tokens_used: int = 0
    output_tokens_used: int = 0
    iterations_used: int = 0
    cost_usd_used: float = 0.0

    # Wall-clock baseline.
    started_at_monotonic: float = field(default_factory=time.monotonic)

    @classmethod
    def for_tier(cls, tier: int) -> "CaseBudget":
        """ADR-038 §8.1 budget table."""
        if tier == 1:
            return cls(
                tier=1,
                max_input_tokens=4_000,
                max_output_tokens=1_000,
                max_iterations=1,
                max_wall_clock_ms=500,
                max_cost_usd=0.001,
            )
        if tier == 2:
            return cls(
                tier=2,
                max_input_tokens=16_000,
                max_output_tokens=4_000,
                max_iterations=6,
                max_wall_clock_ms=8_000,
                max_cost_usd=0.05,
            )
        if tier == 3:
            return cls(
                tier=3,
                max_input_tokens=8_000,
                max_output_tokens=2_000,
                max_iterations=8,
                max_wall_clock_ms=12_000,
                max_cost_usd=0.08,
            )
        raise ValueError(f"Unknown tier: {tier}")

    @classmethod
    def for_case(cls, case: OrderCase) -> "CaseBudget":
        return cls.for_tier(case.tier)

    # ----- accounting -------------------------------------------

    def deduct(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        iterations: int = 0,
    ) -> None:
        self.input_tokens_used += int(input_tokens)
        self.output_tokens_used += int(output_tokens)
        self.iterations_used += int(iterations)
        self.cost_usd_used += float(cost_usd)

    def wall_clock_ms_used(self) -> int:
        return int((time.monotonic() - self.started_at_monotonic) * 1000)

    def is_exhausted(self) -> Optional[str]:
        """Return a reason string when any limit is breached, else None."""
        if self.input_tokens_used >= self.max_input_tokens:
            return f"input_tokens ({self.input_tokens_used} >= {self.max_input_tokens})"
        if self.output_tokens_used >= self.max_output_tokens:
            return f"output_tokens ({self.output_tokens_used} >= {self.max_output_tokens})"
        if self.iterations_used >= self.max_iterations:
            return f"iterations ({self.iterations_used} >= {self.max_iterations})"
        if self.cost_usd_used >= self.max_cost_usd:
            return f"cost_usd ({self.cost_usd_used:.4f} >= {self.max_cost_usd:.4f})"
        if self.wall_clock_ms_used() >= self.max_wall_clock_ms:
            return f"wall_clock_ms (>= {self.max_wall_clock_ms})"
        return None

    def remaining_iterations(self) -> int:
        return max(0, self.max_iterations - self.iterations_used)

    def to_audit_dict(self) -> dict:
        """Snapshot for the audit log."""
        return {
            "tier": self.tier,
            "input_tokens_used": self.input_tokens_used,
            "output_tokens_used": self.output_tokens_used,
            "iterations_used": self.iterations_used,
            "cost_usd_used": round(self.cost_usd_used, 6),
            "wall_clock_ms_used": self.wall_clock_ms_used(),
            "limits": {
                "max_input_tokens": self.max_input_tokens,
                "max_output_tokens": self.max_output_tokens,
                "max_iterations": self.max_iterations,
                "max_wall_clock_ms": self.max_wall_clock_ms,
                "max_cost_usd": self.max_cost_usd,
            },
        }
