"""ADR-045 §2.5 / P2.9 — per-page cost guardrail + cost meter + drift signal.

Document-extraction cost must be CAPPED (a per-page guardrail circuit-breaks
runaway spend) AND queryable/attributable (a cost meter labelled by tenant /
provider / model_id). A production drift signal (model_id + prompt_hash + mean
confidence + canary containment) makes a quiet model regression visible between
nightly evals.
"""

from __future__ import annotations

import pytest

from api import metrics
from contracts.policy import (
    EXTRACTION_MAX_COST_USD_PER_PAGE,
    ExtractionCostExceeded,
    assert_within_page_cost_budget,
)


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset_extraction_metrics()
    yield
    metrics.reset_extraction_metrics()


def test_guardrail_allows_within_budget():
    # No raise when cost/page is under the cap.
    assert_within_page_cost_budget(
        cost_usd=EXTRACTION_MAX_COST_USD_PER_PAGE * 2, pages=4,
    )


def test_guardrail_circuit_breaks_runaway_spend():
    with pytest.raises(ExtractionCostExceeded):
        assert_within_page_cost_budget(
            cost_usd=EXTRACTION_MAX_COST_USD_PER_PAGE * 5, pages=1,
        )


def test_guardrail_rejects_nonpositive_pages():
    with pytest.raises(ValueError):
        assert_within_page_cost_budget(cost_usd=0.01, pages=0)


def test_cost_meter_is_labelled_and_accumulates():
    metrics.record_extraction_cost(
        tenant="tenant-a", provider="textract", model_id="m1", pages=3, cost_usd=0.03,
    )
    metrics.record_extraction_cost(
        tenant="tenant-a", provider="textract", model_id="m1", pages=1, cost_usd=0.01,
    )
    out = metrics.render_extraction_metrics()
    # Prometheus label order is sorted (model_id, provider, tenant).
    assert 'extraction_cost_usd_total{model_id="m1",provider="textract",tenant="tenant-a"} 0.04' in out
    assert 'extraction_pages_total{model_id="m1",provider="textract",tenant="tenant-a"} 4' in out


def test_drift_signal_tracks_confidence_and_containment():
    metrics.record_extraction_drift(
        model_id="m1", prompt_hash="p1", confidence=0.9, containment=1.0,
    )
    metrics.record_extraction_drift(
        model_id="m1", prompt_hash="p1", confidence=0.8, containment=0.9,
    )
    out = metrics.render_extraction_metrics()
    # mean confidence over the window = 0.85; mean containment = 0.95
    assert 'extraction_mean_confidence{model_id="m1",prompt_hash="p1"} 0.85' in out
    assert 'extraction_canary_containment{model_id="m1",prompt_hash="p1"} 0.95' in out


def test_metrics_surface_includes_extraction_series():
    metrics.record_extraction_cost(
        tenant="t", provider="docTR", model_id="m", pages=1, cost_usd=0.005,
    )
    assert "extraction_cost_usd_total" in metrics.render_all()
