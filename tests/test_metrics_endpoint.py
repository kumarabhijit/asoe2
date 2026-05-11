"""ADR-039 §7.3 — Prometheus metrics endpoint tests.

Locks the metric-name + label vocabulary the Grafana dashboard
imports against, so a future rename here triggers a CI failure
rather than a silently-empty dashboard panel.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.metrics import render_shadow_llm_metrics
from compliance.shadow_llm import (
    ShadowLLMMetrics,
    shadow_llm_cache,
    shadow_llm_metrics,
)


@pytest.fixture(autouse=True)
def _reset():
    shadow_llm_metrics.reset()
    shadow_llm_cache.clear()
    yield
    shadow_llm_metrics.reset()
    shadow_llm_cache.clear()


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Pure render_shadow_llm_metrics
# ---------------------------------------------------------------------------

class TestRenderShadowLLMMetrics:
    def test_emits_required_metric_families(self):
        m = ShadowLLMMetrics()
        body = render_shadow_llm_metrics(m)
        # Every metric family the §7.3 dashboard expects.
        for name in (
            "shadow_llm_invocations_total",
            "shadow_llm_invocations_by_trigger",
            "shadow_llm_cache_hits_total",
            "shadow_llm_verdicts_total",
            "shadow_llm_timeouts_total",
            "shadow_llm_unavailable_total",
            "shadow_llm_validation_errors_total",
            "shadow_llm_skipped_red_total",
            "shadow_llm_skipped_below_floor_total",
            "shadow_llm_latency_ms_sum",
            "shadow_llm_latency_ms_count",
            "shadow_llm_cost_usd_total",
            "shadow_llm_disagreement_rate",
            "shadow_llm_abstain_rate",
            "shadow_llm_cache_hit_rate",
            "shadow_llm_avg_latency_ms",
            # ADR-039 §6.3 X.2→X.3 ratification gate.
            "shadow_llm_reviewer_overrides_of_downgrade_total",
            "shadow_llm_reviewer_override_rate_on_downgrades",
        ):
            assert f"# HELP {name} " in body, name
            assert f"# TYPE {name} " in body, name

    def test_verdicts_emitted_for_every_action(self):
        m = ShadowLLMMetrics()
        m.verdicts_by_action["AGREE"] = 5
        body = render_shadow_llm_metrics(m)
        # Every action emits a row, including zero-count ones —
        # so the Grafana panel's `sum by (action)` doesn't drop
        # categories.
        assert 'shadow_llm_verdicts_total{action="AGREE"} 5' in body
        assert 'shadow_llm_verdicts_total{action="DISAGREE_DOWNGRADE"} 0' in body
        assert 'shadow_llm_verdicts_total{action="ABSTAIN"} 0' in body

    def test_invocations_by_trigger_emits_observed_only(self):
        m = ShadowLLMMetrics()
        m.invocations_by_trigger["financial_impact"] = 10
        m.invocations_by_trigger["deterministic_yellow"] = 4
        body = render_shadow_llm_metrics(m)
        assert 'shadow_llm_invocations_by_trigger{trigger="financial_impact"} 10' in body
        assert 'shadow_llm_invocations_by_trigger{trigger="deterministic_yellow"} 4' in body

    def test_derived_rates_at_zero_when_no_invocations(self):
        m = ShadowLLMMetrics()
        body = render_shadow_llm_metrics(m)
        # Rates are 0.0 (not NaN / not missing) when there's no
        # traffic — the dashboard panel needs a value to plot.
        assert "shadow_llm_disagreement_rate 0.0" in body
        assert "shadow_llm_abstain_rate 0.0" in body
        assert "shadow_llm_cache_hit_rate 0.0" in body
        assert "shadow_llm_avg_latency_ms 0.0" in body
        assert "shadow_llm_reviewer_overrides_of_downgrade_total 0" in body
        assert "shadow_llm_reviewer_override_rate_on_downgrades 0.0" in body

    def test_reviewer_override_rate_uses_downgrade_count_as_denominator(self):
        m = ShadowLLMMetrics()
        m.invocations_total = 100
        m.verdicts_by_action["DISAGREE_DOWNGRADE"] = 20
        m.reviewer_overrides_of_llm_downgrade_total = 5
        # 5 / 20 = 0.25 — not 5 / 100. The X.2→X.3 gate's denominator
        # is the L2-issued downgrade count, not total invocations.
        assert m.reviewer_override_rate_on_llm_downgrades() == 0.25
        body = render_shadow_llm_metrics(m)
        assert "shadow_llm_reviewer_overrides_of_downgrade_total 5" in body
        assert "shadow_llm_reviewer_override_rate_on_downgrades 0.25" in body

    def test_reviewer_override_rate_is_zero_when_no_downgrades(self):
        m = ShadowLLMMetrics()
        # No DISAGREE_DOWNGRADE verdicts → denominator 0; the rate
        # must be 0.0 (no signal), NOT a division-by-zero or 100%.
        m.reviewer_overrides_of_llm_downgrade_total = 3
        assert m.reviewer_override_rate_on_llm_downgrades() == 0.0

    def test_label_value_escaping(self):
        m = ShadowLLMMetrics()
        # Defence-in-depth: a trigger with quote / backslash in
        # its name shouldn't break the format. The trigger
        # vocabulary is closed (we never get arbitrary strings)
        # but render must be safe regardless.
        m.invocations_by_trigger['weird"trigger\\name'] = 1
        body = render_shadow_llm_metrics(m)
        assert r'weird\"trigger\\name' in body


# ---------------------------------------------------------------------------
# /api/v1/metrics route
# ---------------------------------------------------------------------------

class TestMetricsRoute:
    def test_no_auth_required(self, client):
        # Prometheus scrapers don't carry JWTs; the endpoint is
        # public-by-design (the scrape happens inside the
        # cluster network).
        r = client.get("/api/v1/metrics")
        assert r.status_code == 200

    def test_content_type_matches_prometheus_spec(self, client):
        r = client.get("/api/v1/metrics")
        assert r.headers["content-type"].startswith(
            "text/plain; version=0.0.4",
        )

    def test_body_contains_zero_baseline(self, client):
        r = client.get("/api/v1/metrics")
        assert "shadow_llm_invocations_total 0" in r.text

    def test_body_reflects_module_state(self, client):
        # Mutate the singleton and confirm the next scrape
        # observes the new counters.
        shadow_llm_metrics.invocations_total = 7
        shadow_llm_metrics.cache_hits_total = 2
        shadow_llm_metrics.verdicts_by_action["DISAGREE_DOWNGRADE"] = 3
        r = client.get("/api/v1/metrics")
        assert "shadow_llm_invocations_total 7" in r.text
        assert "shadow_llm_cache_hits_total 2" in r.text
        assert 'shadow_llm_verdicts_total{action="DISAGREE_DOWNGRADE"} 3' in r.text

    def test_endpoint_excluded_from_openapi(self, client):
        # Prometheus scrape isn't part of the contract the UI
        # consumes; keep it out of the openapi schema so
        # asoe-ui's drift gate doesn't churn.
        spec = client.get("/openapi.json").json()
        paths = spec.get("paths", {})
        assert "/api/v1/metrics" not in paths
