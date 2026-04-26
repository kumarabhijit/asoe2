from __future__ import annotations

# Daily USD spend tracker for the LLM tier.
#
# Two backends:
#   - RedisBudgetTracker — production. Counter key
#     `asoe:llm:budget:{date}` with a 48h TTL. Atomic INCRBYFLOAT;
#     read-modify-write race is impossible across worker pods.
#   - InMemoryBudgetTracker — dev/test fallback. Per-process counter
#     keyed by date. NOT safe across pods; CI/dev only.
#
# Cost is computed from observed token counts × the pricing table in
# contracts/policy.py. The tracker exposes a `BudgetState` snapshot
# (consumed_usd, budget_usd, hard_block, soft_warn) that the router
# reads BEFORE every LLM call. When `hard_block` is True the router
# routes to the deterministic fallback for the rest of the UTC day
# (no graph run is failed by budget exhaustion — we degrade safely).
#
# Cost/Ops review (panel) flagged this as a blocker for V1 PR-1.

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Iterable

from contracts.policy import (
    LLM_BUDGET_HARD_BLOCK_PCT,
    LLM_BUDGET_SOFT_WARN_PCT,
    LLM_DAILY_USD_BUDGET_DEFAULT,
    LLM_PRICING_USD_PER_M_TOKENS,
)

logger = logging.getLogger("asoe.llm.budget")


@dataclass(frozen=True)
class BudgetState:
    """Snapshot of the daily LLM budget for a UTC date.

    `hard_block` is the only field the router needs to gate calls.
    The rest are exposed for telemetry and observability.
    """

    date_iso: str
    consumed_usd: float
    budget_usd: float
    consumed_pct: float
    soft_warn: bool
    hard_block: bool


@dataclass(frozen=True)
class TokenUsage:
    """Token counts as reported by Anthropic in `response.usage`."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def estimate_cost_usd(model_id: str, usage: TokenUsage) -> float:
    """Convert observed token counts into a USD spend estimate.

    Pricing comes from LLM_PRICING_USD_PER_M_TOKENS in contracts/policy.py.
    Unknown models cost 0.0 (can't bill what we can't price; logged as
    a warning so a model-id drift doesn't silently underbill).
    """
    rates = LLM_PRICING_USD_PER_M_TOKENS.get(model_id)
    if rates is None:
        logger.warning(
            "llm.budget.unknown_model",
            extra={"model_id": model_id, "reason": "missing_from_pricing_table"},
        )
        return 0.0
    per_m = 1_000_000.0
    return (
        usage.input_tokens * rates["input"] / per_m
        + usage.output_tokens * rates["output"] / per_m
        + usage.cache_read_input_tokens * rates["cache_read"] / per_m
        + usage.cache_creation_input_tokens * rates["cache_write_5m"] / per_m
    )


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _build_state(consumed: float, budget: float, date_iso: str) -> BudgetState:
    pct = (consumed / budget) if budget > 0 else float("inf")
    return BudgetState(
        date_iso=date_iso,
        consumed_usd=round(consumed, 6),
        budget_usd=round(budget, 6),
        consumed_pct=pct,
        soft_warn=pct >= LLM_BUDGET_SOFT_WARN_PCT,
        hard_block=pct >= LLM_BUDGET_HARD_BLOCK_PCT,
    )


class InMemoryBudgetTracker:
    """Per-process daily counter. Single-pod / dev / test only."""

    def __init__(self, daily_budget_usd: float = LLM_DAILY_USD_BUDGET_DEFAULT):
        self._daily_budget_usd = daily_budget_usd
        self._lock = Lock()
        self._counts: dict[str, float] = {}

    @property
    def daily_budget_usd(self) -> float:
        return self._daily_budget_usd

    def consume(self, amount_usd: float) -> BudgetState:
        if amount_usd < 0:
            raise ValueError("amount_usd must be non-negative")
        date_iso = _today_utc_iso()
        with self._lock:
            self._counts[date_iso] = self._counts.get(date_iso, 0.0) + amount_usd
            consumed = self._counts[date_iso]
        return _build_state(consumed, self._daily_budget_usd, date_iso)

    def snapshot(self) -> BudgetState:
        date_iso = _today_utc_iso()
        with self._lock:
            consumed = self._counts.get(date_iso, 0.0)
        return _build_state(consumed, self._daily_budget_usd, date_iso)

    def reset(self) -> None:
        """Test helper. Clears all counters."""
        with self._lock:
            self._counts.clear()


class RedisBudgetTracker:
    """Multi-pod daily counter backed by Redis INCRBYFLOAT.

    Counter key: `asoe:llm:budget:{YYYY-MM-DD}` with 48h TTL so two
    days of history are queryable for incident postmortems but the
    key auto-expires.
    """

    KEY_PREFIX = "asoe:llm:budget:"
    TTL_SECONDS = 48 * 60 * 60  # 48h, comfortably more than 24h to retain yesterday

    def __init__(
        self,
        client,  # redis.Redis-like; typed as Any to avoid import at module level
        daily_budget_usd: float = LLM_DAILY_USD_BUDGET_DEFAULT,
    ):
        self._client = client
        self._daily_budget_usd = daily_budget_usd

    @property
    def daily_budget_usd(self) -> float:
        return self._daily_budget_usd

    def _key(self, date_iso: str) -> str:
        return f"{self.KEY_PREFIX}{date_iso}"

    def consume(self, amount_usd: float) -> BudgetState:
        if amount_usd < 0:
            raise ValueError("amount_usd must be non-negative")
        date_iso = _today_utc_iso()
        key = self._key(date_iso)
        try:
            new_total = self._client.incrbyfloat(key, amount_usd)
            self._client.expire(key, self.TTL_SECONDS)
            consumed = float(new_total)
        except Exception as exc:  # noqa: BLE001
            # Redis hiccup must not block the LLM call itself — the
            # router has its own budget read; we just log and assume
            # the increment was lost. Worst case: a few cents of
            # under-counting during a Redis outage.
            logger.warning(
                "llm.budget.redis_consume_failed",
                extra={"error": str(exc), "amount_usd": amount_usd, "date": date_iso},
            )
            consumed = 0.0
        return _build_state(consumed, self._daily_budget_usd, date_iso)

    def snapshot(self) -> BudgetState:
        date_iso = _today_utc_iso()
        key = self._key(date_iso)
        try:
            raw = self._client.get(key)
            consumed = float(raw) if raw else 0.0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "llm.budget.redis_snapshot_failed",
                extra={"error": str(exc), "date": date_iso},
            )
            consumed = 0.0
        return _build_state(consumed, self._daily_budget_usd, date_iso)


def create_budget_tracker(
    daily_budget_usd: float | None = None,
) -> InMemoryBudgetTracker | RedisBudgetTracker:
    """Factory that picks the backend by REDIS_URL presence.

    The daily budget defaults to ASOE_LLM_DAILY_USD_BUDGET when set,
    otherwise LLM_DAILY_USD_BUDGET_DEFAULT (5.0 USD).
    """
    if daily_budget_usd is None:
        raw = os.getenv("ASOE_LLM_DAILY_USD_BUDGET", "").strip()
        daily_budget_usd = float(raw) if raw else LLM_DAILY_USD_BUDGET_DEFAULT

    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            import redis  # noqa: PLC0415

            client = redis.from_url(redis_url, decode_responses=True)
            return RedisBudgetTracker(client, daily_budget_usd)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "llm.budget.redis_init_failed",
                extra={"error": str(exc)},
            )
            # Fall through to in-memory; never crash the import path
            # because of an unreachable Redis.
    return InMemoryBudgetTracker(daily_budget_usd)


# Module-level singleton constructed lazily on first use; mirrors the
# api/pubsub.py pattern so the LLM router can call .snapshot() without
# threading a tracker through every layer.
_singleton: InMemoryBudgetTracker | RedisBudgetTracker | None = None
_singleton_lock = Lock()


def get_budget_tracker() -> InMemoryBudgetTracker | RedisBudgetTracker:
    """Return the process-wide budget tracker singleton."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = create_budget_tracker()
    return _singleton


def reset_budget_tracker() -> None:
    """Test helper; clear the singleton so a fresh tracker is built."""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__: Iterable[str] = (
    "BudgetState",
    "InMemoryBudgetTracker",
    "RedisBudgetTracker",
    "TokenUsage",
    "create_budget_tracker",
    "estimate_cost_usd",
    "get_budget_tracker",
    "reset_budget_tracker",
)
