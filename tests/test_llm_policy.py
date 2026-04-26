from __future__ import annotations

# Coverage for the LLM-related constants in contracts/policy.py
#
# These constants are import-time references for the LLM router,
# budget tracker, and circuit breaker. The tests assert their values
# and shape so a typo (e.g. dropping a model from the pricing table)
# fails CI rather than silently mis-pricing a request.

from contracts.policy import (
    LLM_BUDGET_HARD_BLOCK_PCT,
    LLM_BUDGET_SOFT_WARN_PCT,
    LLM_CALL_TIMEOUT_S,
    LLM_CIRCUIT_BREAKER_COOLDOWN_S,
    LLM_CIRCUIT_BREAKER_ERROR_RATE_PCT,
    LLM_CIRCUIT_BREAKER_P95_LATENCY_S,
    LLM_CROSS_CHECK_DISAGREEMENT_REASON,
    LLM_DAILY_USD_BUDGET_DEFAULT,
    LLM_DEFAULT_MODEL_ID,
    LLM_PER_RUN_USD_CAP,
    LLM_PRICING_USD_PER_M_TOKENS,
    LLM_PROVIDER_DEFAULT,
)


def test_llm_provider_default_is_fallback() -> None:
    assert LLM_PROVIDER_DEFAULT == "fallback"


def test_llm_default_model_is_sonnet_4_6() -> None:
    # Aligns with architecture_v3.md §4.2 (Reasoning Core: Claude 4.6 Sonnet)
    assert LLM_DEFAULT_MODEL_ID == "claude-sonnet-4-6"


def test_daily_budget_default_is_five_dollars() -> None:
    # Sandbox shakeout sizing — see panel synthesis notes.
    assert LLM_DAILY_USD_BUDGET_DEFAULT == 5.0


def test_budget_thresholds_ordered() -> None:
    assert 0 < LLM_BUDGET_SOFT_WARN_PCT < LLM_BUDGET_HARD_BLOCK_PCT <= 1.0


def test_circuit_breaker_thresholds_positive() -> None:
    assert 0 < LLM_CIRCUIT_BREAKER_ERROR_RATE_PCT < 1.0
    assert LLM_CIRCUIT_BREAKER_P95_LATENCY_S > 0
    assert LLM_CIRCUIT_BREAKER_COOLDOWN_S > 0


def test_call_timeout_under_p50_sla() -> None:
    # 3 calls × 3 SDK retries × timeout must fit comfortably under
    # the 8-min p50 SLA from architecture_v3.md §2.
    assert LLM_CALL_TIMEOUT_S * 3 * 3 < 8 * 60


def test_per_run_cap_is_positive() -> None:
    assert LLM_PER_RUN_USD_CAP > 0


def test_disagreement_reason_is_terminal_compatible() -> None:
    # The router uses this string verbatim as the explanation for
    # MANUAL_REVIEW_REQUIRED — which is a valid terminal status per
    # CLAUDE.md §5. Keep the spelling stable.
    assert LLM_CROSS_CHECK_DISAGREEMENT_REASON == "LLM_DETERMINISTIC_DISAGREEMENT"


def test_pricing_table_includes_default_model() -> None:
    assert LLM_DEFAULT_MODEL_ID in LLM_PRICING_USD_PER_M_TOKENS


def test_pricing_table_kinds_complete() -> None:
    required = {"input", "output", "cache_read", "cache_write_5m"}
    for model, rates in LLM_PRICING_USD_PER_M_TOKENS.items():
        assert required.issubset(rates.keys()), (
            f"Pricing for {model} missing required keys {required - rates.keys()}"
        )
        # Sanity: prices are positive USD/M tokens.
        for kind, price in rates.items():
            assert price > 0, f"{model}.{kind} = {price}"


def test_pricing_table_cache_read_cheaper_than_input() -> None:
    """Cache reads bill at ~0.1× input. Sanity-check the relationship
    so a copy-paste error in the table can't silently overcharge."""
    for model, rates in LLM_PRICING_USD_PER_M_TOKENS.items():
        assert rates["cache_read"] < rates["input"], model


def test_pricing_table_cache_write_premium_over_input() -> None:
    """5-minute cache writes bill at ~1.25× input (write premium)."""
    for model, rates in LLM_PRICING_USD_PER_M_TOKENS.items():
        assert rates["cache_write_5m"] > rates["input"], model
