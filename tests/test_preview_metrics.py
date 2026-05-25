"""CP-B RED gate (ADR-043 §2.6) — preview/highlight observability is a DoD.

Test-first (`xfail(strict=True)`; removed at CP-C). The feature is not done
unless on-call can see it: a highlight that silently lands wrong is the panel's
#1 hazard, and the leading indicator is the `highlight_outcome` ratio. Mirrors
`test_metrics_endpoint.py` (scrape `/api/v1/metrics`). Also locks D7's bounded
cardinality: no per-document id may appear as a Prometheus label.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import create_app


def test_metrics_expose_highlight_and_preview_families_with_bounded_labels():
    client = TestClient(create_app(), raise_server_exceptions=False)
    body = client.get("/api/v1/metrics").text
    for name in (
        "highlight_outcome_total",   # {result=located|unlocated|ambiguous, mime}
        "preview_render_total",      # {result, mime}
        "preview_render_latency_ms",
    ):
        assert f"# TYPE {name} " in body, name
    # Bounded cardinality: attachment_id / case_id must never be a label.
    assert "attachment_id=" not in body
    assert "case_id=" not in body
