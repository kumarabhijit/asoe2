"""DoR gate #11 — automation-bias SLIs (Layer-2-open rate + decision dwell).

The reviewer-override rate already ships on the shadow-LLM surface; this adds the
two behavioural rubber-stamping signals. The UI reports one event per decision
to POST /api/v1/metrics/reviewer-activity; the metrics surface exposes the
counters + a dwell histogram + the Layer-2-open rate gauge.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import metrics
from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset_reviewer_activity()
    yield
    metrics.reset_reviewer_activity()


@pytest.fixture()
def client():
    exception_store.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(roles=("analyst",)):
    return {"Authorization": f"Bearer {create_test_token(roles=list(roles), org='tenant-a')}"}


# --- unit: the metric surface --------------------------------------------

def test_record_increments_decisions_layer2_and_dwell():
    metrics.record_reviewer_activity(dwell_ms=2000, layer2_opened=True)
    metrics.record_reviewer_activity(dwell_ms=500, layer2_opened=False)
    out = metrics.render_reviewer_activity_metrics()
    assert "reviewer_decisions_total 2" in out
    assert "reviewer_layer2_opened_total 1" in out
    assert "reviewer_layer2_open_rate 0.5" in out
    # dwell: 0.5s in le="1", 2s in le="3" → cumulative le="3" == 2
    assert 'reviewer_decision_dwell_seconds_bucket{le="1"} 1' in out
    assert 'reviewer_decision_dwell_seconds_bucket{le="3"} 2' in out
    assert "reviewer_decision_dwell_seconds_count 2" in out


def test_highlight_shown_cohort_is_tracked_separately():
    # ADR-043 §2.7 / D12 — the decision-quality signal: scrutiny must be
    # comparable between decisions where a highlight was shown and where it
    # wasn't, so a drop in scrutiny under highlighting registers as a
    # regression, not a win.
    metrics.record_reviewer_activity(dwell_ms=2000, layer2_opened=True, highlight_shown=True)
    metrics.record_reviewer_activity(dwell_ms=500, layer2_opened=False, highlight_shown=True)
    metrics.record_reviewer_activity(dwell_ms=3000, layer2_opened=True, highlight_shown=False)
    out = metrics.render_reviewer_activity_metrics()
    # aggregate series unchanged
    assert "reviewer_decisions_total 3" in out
    # per-cohort decisions
    assert 'reviewer_decisions_by_highlight_total{highlight_shown="true"} 2' in out
    assert 'reviewer_decisions_by_highlight_total{highlight_shown="false"} 1' in out
    # per-cohort Layer-2-open rate (highlight-shown: 1/2; not-shown: 1/1)
    assert 'reviewer_layer2_open_rate_by_highlight{highlight_shown="true"} 0.5' in out
    assert 'reviewer_layer2_open_rate_by_highlight{highlight_shown="false"} 1.0' in out


def test_highlight_shown_defaults_false():
    metrics.record_reviewer_activity(dwell_ms=1000, layer2_opened=True)
    out = metrics.render_reviewer_activity_metrics()
    assert 'reviewer_decisions_by_highlight_total{highlight_shown="false"} 1' in out


def test_endpoint_accepts_highlight_shown(client: TestClient):
    res = client.post(
        "/api/v1/metrics/reviewer-activity",
        json={"dwell_ms": 1200, "layer2_opened": True, "highlight_shown": True},
        headers=_auth(),
    )
    assert res.status_code == 202, res.text
    scrape = client.get("/api/v1/metrics").text
    assert 'reviewer_decisions_by_highlight_total{highlight_shown="true"} 1' in scrape


def test_bad_dwell_is_a_safe_noop():
    metrics.record_reviewer_activity(dwell_ms=-5, layer2_opened=True)
    metrics.record_reviewer_activity(dwell_ms=float("nan"), layer2_opened=True)
    assert "reviewer_decisions_total 0" in metrics.render_reviewer_activity_metrics()


def test_rate_is_zero_with_no_decisions():
    assert "reviewer_layer2_open_rate 0.0" in metrics.render_reviewer_activity_metrics()


# --- endpoint contract ----------------------------------------------------

def test_endpoint_records_activity_and_surfaces_on_metrics(client: TestClient):
    res = client.post(
        "/api/v1/metrics/reviewer-activity",
        json={"dwell_ms": 4200, "layer2_opened": True},
        headers=_auth(),
    )
    assert res.status_code == 202, res.text
    scrape = client.get("/api/v1/metrics").text
    assert "reviewer_decisions_total 1" in scrape
    assert "reviewer_layer2_opened_total 1" in scrape


def test_endpoint_requires_auth(client: TestClient):
    res = client.post(
        "/api/v1/metrics/reviewer-activity",
        json={"dwell_ms": 100, "layer2_opened": False},
    )
    assert res.status_code in (401, 403)


def test_endpoint_rejects_unknown_fields(client: TestClient):
    res = client.post(
        "/api/v1/metrics/reviewer-activity",
        json={"dwell_ms": 100, "layer2_opened": False, "smuggled": 1},
        headers=_auth(),
    )
    assert res.status_code == 422
