"""DoR gate #7 (Phase 8) — ingest→terminal latency SLO histogram.

Locks the histogram mechanism (observe → render) and that it is surfaced on the
Prometheus exposition via `render_all`. The live latency feed is wired at the
synchronous resolve dispatch (`_resolve_state`); here we lock the metric is a
well-formed cumulative histogram and fails closed on bad input.
"""

from __future__ import annotations

import pytest

from api import metrics


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset_slo_histogram()
    yield
    metrics.reset_slo_histogram()


def test_observe_accumulates_into_cumulative_buckets() -> None:
    for s in (0.04, 0.2, 0.2, 3.0, 120.0):
        metrics.observe_ingest_to_terminal_latency(s)
    out = metrics.render_ingest_terminal_histogram()
    name = "asoe_ingest_to_terminal_latency_seconds"
    # Cumulative: le="0.05" caught the 0.04 sample only.
    assert f'{name}_bucket{{le="0.05"}} 1' in out
    # le="0.25" is cumulative: 0.04 + 0.2 + 0.2 = 3 samples.
    assert f'{name}_bucket{{le="0.25"}} 3' in out
    # +Inf catches everything (the 120s tail included).
    assert f'{name}_bucket{{le="+Inf"}} 5' in out
    assert f"{name}_count 5" in out
    assert f"{name}_sum 123.44" in out


def test_render_declares_histogram_type() -> None:
    out = metrics.render_ingest_terminal_histogram()
    assert "# TYPE asoe_ingest_to_terminal_latency_seconds histogram" in out


def test_bad_input_is_a_safe_noop() -> None:
    metrics.observe_ingest_to_terminal_latency(-1.0)
    metrics.observe_ingest_to_terminal_latency(float("nan"))
    metrics.observe_ingest_to_terminal_latency("not-a-number")  # type: ignore[arg-type]
    out = metrics.render_ingest_terminal_histogram()
    assert "asoe_ingest_to_terminal_latency_seconds_count 0" in out


def test_render_all_includes_the_histogram() -> None:
    metrics.observe_ingest_to_terminal_latency(0.5)
    assert "asoe_ingest_to_terminal_latency_seconds_bucket" in metrics.render_all()
