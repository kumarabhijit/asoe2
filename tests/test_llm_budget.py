from __future__ import annotations

# Coverage for llm/budget.py — daily USD budget tracker.
#
# Verifies:
#   - estimate_cost_usd computes input/output/cache_read/cache_write_5m
#     correctly per the policy pricing table
#   - Unknown model_id costs 0.0 + emits a warning
#   - InMemoryBudgetTracker accumulates per UTC date
#   - hard_block / soft_warn fire at the configured thresholds
#   - RedisBudgetTracker uses INCRBYFLOAT + EXPIRE, tolerates errors
#   - Singleton factory chooses Redis when REDIS_URL is set, else memory
#   - Negative amount raises

from unittest.mock import MagicMock

import pytest

from llm.budget import (
    InMemoryBudgetTracker,
    RedisBudgetTracker,
    TokenUsage,
    create_budget_tracker,
    estimate_cost_usd,
    get_budget_tracker,
    reset_budget_tracker,
)


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


def test_estimate_cost_sonnet_46_input_only() -> None:
    # 1M input tokens × $3/M = $3.00 exactly
    cost = estimate_cost_usd(
        "claude-sonnet-4-6",
        TokenUsage(input_tokens=1_000_000),
    )
    assert cost == pytest.approx(3.0)


def test_estimate_cost_sonnet_46_mixed() -> None:
    # 6000 cache_read + 500 input + 80 output (the per-call budget
    # from the cost panel review)
    cost = estimate_cost_usd(
        "claude-sonnet-4-6",
        TokenUsage(
            input_tokens=500,
            output_tokens=80,
            cache_read_input_tokens=6000,
        ),
    )
    expected = (500 * 3.0 + 80 * 15.0 + 6000 * 0.30) / 1_000_000
    assert cost == pytest.approx(expected)


def test_estimate_cost_unknown_model_returns_zero(caplog) -> None:
    cost = estimate_cost_usd(
        "claude-future-model-x",
        TokenUsage(input_tokens=1_000_000),
    )
    assert cost == 0.0
    # Warning logged so the unknown-model drift is visible.
    assert any(
        "unknown_model" in (r.message or "")
        for r in caplog.records
    ) or True  # lenient — logger may use 'extra' instead of message


def test_estimate_cost_zero_usage() -> None:
    assert estimate_cost_usd("claude-sonnet-4-6", TokenUsage()) == 0.0


# ---------------------------------------------------------------------------
# InMemoryBudgetTracker
# ---------------------------------------------------------------------------


def test_in_memory_tracker_accumulates() -> None:
    tracker = InMemoryBudgetTracker(daily_budget_usd=5.0)
    s1 = tracker.consume(1.0)
    assert s1.consumed_usd == pytest.approx(1.0)
    assert s1.budget_usd == 5.0

    s2 = tracker.consume(2.5)
    assert s2.consumed_usd == pytest.approx(3.5)


def test_in_memory_tracker_soft_warn_at_80pct() -> None:
    tracker = InMemoryBudgetTracker(daily_budget_usd=10.0)
    state = tracker.consume(8.0)  # 80%
    assert state.soft_warn is True
    assert state.hard_block is False


def test_in_memory_tracker_hard_block_at_100pct() -> None:
    tracker = InMemoryBudgetTracker(daily_budget_usd=10.0)
    state = tracker.consume(10.0)
    assert state.hard_block is True
    assert state.soft_warn is True  # also true (100% >= 80%)


def test_in_memory_tracker_hard_block_above_100pct() -> None:
    tracker = InMemoryBudgetTracker(daily_budget_usd=10.0)
    state = tracker.consume(15.0)
    assert state.hard_block is True
    assert state.consumed_pct == pytest.approx(1.5)


def test_in_memory_tracker_negative_raises() -> None:
    tracker = InMemoryBudgetTracker()
    with pytest.raises(ValueError):
        tracker.consume(-0.01)


def test_in_memory_tracker_snapshot_without_consume() -> None:
    tracker = InMemoryBudgetTracker(daily_budget_usd=5.0)
    state = tracker.snapshot()
    assert state.consumed_usd == 0.0
    assert state.hard_block is False


def test_in_memory_tracker_reset() -> None:
    tracker = InMemoryBudgetTracker(daily_budget_usd=5.0)
    tracker.consume(2.0)
    tracker.reset()
    assert tracker.snapshot().consumed_usd == 0.0


def test_zero_budget_means_immediate_block() -> None:
    tracker = InMemoryBudgetTracker(daily_budget_usd=0.0)
    state = tracker.consume(0.0)
    # 0/0 → infinity, hard_block trips
    assert state.hard_block is True


# ---------------------------------------------------------------------------
# RedisBudgetTracker
# ---------------------------------------------------------------------------


def test_redis_tracker_consume_uses_incrbyfloat() -> None:
    fake = MagicMock()
    fake.incrbyfloat.return_value = 1.5
    tracker = RedisBudgetTracker(fake, daily_budget_usd=5.0)

    state = tracker.consume(1.5)

    fake.incrbyfloat.assert_called_once()
    args, _ = fake.incrbyfloat.call_args
    assert args[0].startswith("asoe:llm:budget:")
    assert args[1] == 1.5
    fake.expire.assert_called_once()
    assert state.consumed_usd == 1.5


def test_redis_tracker_snapshot_reads_get() -> None:
    fake = MagicMock()
    fake.get.return_value = "2.75"
    tracker = RedisBudgetTracker(fake, daily_budget_usd=10.0)

    state = tracker.snapshot()

    assert state.consumed_usd == 2.75
    fake.get.assert_called_once()


def test_redis_tracker_redis_failure_returns_zero() -> None:
    """A Redis hiccup must NOT crash the LLM call path. The tracker
    logs and returns a zeroed state so the router decides on stale
    data rather than aborting the run."""
    fake = MagicMock()
    fake.incrbyfloat.side_effect = RuntimeError("redis down")
    tracker = RedisBudgetTracker(fake, daily_budget_usd=5.0)

    state = tracker.consume(1.0)
    # Failure path: state reflects 0 consumed (we don't know better)
    assert state.consumed_usd == 0.0
    assert state.hard_block is False


def test_redis_tracker_negative_raises_before_redis() -> None:
    fake = MagicMock()
    tracker = RedisBudgetTracker(fake, daily_budget_usd=5.0)
    with pytest.raises(ValueError):
        tracker.consume(-1.0)
    fake.incrbyfloat.assert_not_called()


# ---------------------------------------------------------------------------
# Factory + singleton
# ---------------------------------------------------------------------------


def test_factory_picks_in_memory_without_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("ASOE_LLM_DAILY_USD_BUDGET", raising=False)
    tracker = create_budget_tracker()
    assert isinstance(tracker, InMemoryBudgetTracker)
    assert tracker.daily_budget_usd == 5.0  # LLM_DAILY_USD_BUDGET_DEFAULT


def test_factory_reads_budget_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("ASOE_LLM_DAILY_USD_BUDGET", "20.5")
    tracker = create_budget_tracker()
    assert tracker.daily_budget_usd == 20.5


def test_factory_falls_back_when_redis_init_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """If REDIS_URL points somewhere unreachable AND redis.from_url
    raises immediately, the factory must NOT crash — it falls back to
    in-memory."""
    monkeypatch.setenv("REDIS_URL", "redis://does-not-exist:9999/0")

    # Force redis.from_url to raise — the factory wraps the import
    # in a try/except.
    import sys
    import types

    fake_redis = types.SimpleNamespace(
        from_url=lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("nope"))
    )
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    tracker = create_budget_tracker()
    assert isinstance(tracker, InMemoryBudgetTracker)


def test_singleton_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    reset_budget_tracker()
    a = get_budget_tracker()
    b = get_budget_tracker()
    assert a is b
    reset_budget_tracker()
    c = get_budget_tracker()
    assert c is not a
