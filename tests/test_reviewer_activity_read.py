"""Review-Quality read surface (Phase 4 real-data slice).

Two halves:
  1. `reviewer_activity_snapshot()` is a faithful, deterministic projection of
     the in-process automation-bias SLI counters.
  2. `GET /api/v1/metrics/reviewer-activity` returns it, manager+/admin only.

No fabricated values: the console reads ONLY these real counters. Counterfactual
STP and the calibration reliability diagram are deliberately absent (no
request-time data source yet).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.metrics import (
    record_reviewer_activity,
    reset_reviewer_activity,
    reviewer_activity_snapshot,
)


@pytest.fixture(autouse=True)
def _clean_counters():
    reset_reviewer_activity()
    yield
    reset_reviewer_activity()


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(roles):
    return {"Authorization": f"Bearer {create_test_token(roles=roles, org='tenant-a')}"}


class TestSnapshot:
    def test_empty_snapshot_is_zeroed_not_fabricated(self):
        snap = reviewer_activity_snapshot()
        assert snap["decisions"] == 0
        assert snap["layer2_open_rate"] == 0.0
        assert snap["scope"] == "process_local_since_restart"

    def test_layer2_open_rate_and_highlight_cohorts(self):
        # 3 decisions: 2 with an evidence highlight shown (1 scrutinised),
        # 1 without a highlight (scrutinised).
        record_reviewer_activity(dwell_ms=4000, layer2_opened=True, highlight_shown=True)
        record_reviewer_activity(dwell_ms=800, layer2_opened=False, highlight_shown=True)
        record_reviewer_activity(dwell_ms=9000, layer2_opened=True, highlight_shown=False)

        snap = reviewer_activity_snapshot()
        assert snap["decisions"] == 3
        assert snap["layer2_opened"] == 2
        assert snap["layer2_open_rate"] == round(2 / 3, 4)
        # Cohort split (the ADR-043 automation-bias regression signal).
        assert snap["by_highlight"]["shown"]["decisions"] == 2
        assert snap["by_highlight"]["shown"]["layer2_open_rate"] == 0.5
        assert snap["by_highlight"]["not_shown"]["layer2_open_rate"] == 1.0
        # Dwell histogram is cumulative and totals all decisions at +Inf.
        assert snap["dwell_seconds_histogram"][-1]["count"] == 3
        assert snap["dwell_seconds_histogram"][-1]["le_seconds"] is None


class TestEndpoint:
    def test_manager_reads_snapshot(self, client):
        record_reviewer_activity(dwell_ms=4000, layer2_opened=True)
        r = client.get("/api/v1/metrics/reviewer-activity", headers=_auth(["manager"]))
        assert r.status_code == 200
        body = r.json()
        assert body["decisions"] == 1
        assert body["scope"] == "process_local_since_restart"

    def test_analyst_forbidden(self, client):
        r = client.get("/api/v1/metrics/reviewer-activity", headers=_auth(["analyst"]))
        assert r.status_code == 403
