"""ADR-041 P3e Phase 3 sign-off gate #5 — cases V2 telemetry route tests.

Covers:
  * Happy-path 202 with valid payloads for all three endpoints.
  * Pydantic validation rejection for malformed payloads.
  * Role gating — viewer can read /cases but cannot post telemetry.
  * Counter wiring — record_* helpers fire on accepted requests.
  * Prometheus rendering — all three counter classes show up in
    the /metrics text output.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api import metrics as metrics_mod


@pytest.fixture()
def client():
    if hasattr(metrics_mod, "reset_cases_v2_telemetry"):
        metrics_mod.reset_cases_v2_telemetry()
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    return create_test_token(roles=["analyst"], org="tenant-a")


@pytest.fixture()
def viewer_token():
    return create_test_token(roles=["viewer"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Row-click endpoint
# ---------------------------------------------------------------------------


class TestRowClick:
    def test_happy_path_returns_202(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/row-click",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "row_index": 3,
                "audit_verdict_color": "R",
            },
        )
        assert r.status_code == 202
        assert r.json() == {"ok": True}

    def test_null_verdict_accepted(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/row-click",
            headers=_auth(analyst_token),
            json={"case_id": "case-1", "audit_verdict_color": None},
        )
        assert r.status_code == 202

    def test_invalid_verdict_rejected(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/row-click",
            headers=_auth(analyst_token),
            json={"case_id": "case-1", "audit_verdict_color": "Z"},
        )
        assert r.status_code == 422

    def test_missing_case_id_rejected(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/row-click",
            headers=_auth(analyst_token),
            json={"row_index": 0},
        )
        assert r.status_code == 422

    def test_viewer_forbidden(self, client, viewer_token):
        r = client.post(
            "/api/v1/metrics/cases/row-click",
            headers=_auth(viewer_token),
            json={"case_id": "case-1"},
        )
        # require_role("analyst", "manager", "admin") — viewer is
        # not in the allowlist.
        assert r.status_code == 403

    def test_records_into_counter(self, client, analyst_token):
        initial = metrics_mod._cases_v2_row_clicks
        r = client.post(
            "/api/v1/metrics/cases/row-click",
            headers=_auth(analyst_token),
            json={"case_id": "case-1", "audit_verdict_color": "R"},
        )
        assert r.status_code == 202
        assert metrics_mod._cases_v2_row_clicks == initial + 1
        assert metrics_mod._cases_v2_row_clicks_by_verdict["R"] >= 1


# ---------------------------------------------------------------------------
# Time-to-first-action endpoint
# ---------------------------------------------------------------------------


class TestTimeToFirstAction:
    def test_happy_path_returns_202(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/time-to-first-action",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": 4500,
                "first_action": "approve",
            },
        )
        assert r.status_code == 202

    def test_records_into_counter(self, client, analyst_token):
        initial = metrics_mod._cases_v2_ttfa_count
        r = client.post(
            "/api/v1/metrics/cases/time-to-first-action",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": 1200,
                "first_action": "escalate",
            },
        )
        assert r.status_code == 202
        assert metrics_mod._cases_v2_ttfa_count == initial + 1
        assert metrics_mod._cases_v2_ttfa_by_action["escalate"] >= 1

    @pytest.mark.parametrize(
        "first_action,expected_status",
        [
            ("approve", 202),
            ("reject", 202),
            ("override", 202),
            ("escalate", 202),
            ("reanalyze", 202),
            ("acknowledge", 422),  # not a legal HITL action
            ("APPROVE", 422),       # case-sensitive
        ],
    )
    def test_first_action_vocab(
        self, client, analyst_token, first_action, expected_status,
    ):
        r = client.post(
            "/api/v1/metrics/cases/time-to-first-action",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": 500,
                "first_action": first_action,
            },
        )
        assert r.status_code == expected_status

    def test_negative_dwell_rejected(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/time-to-first-action",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": -100,
                "first_action": "approve",
            },
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Time-to-action-submit endpoint
# ---------------------------------------------------------------------------


class TestTimeToActionSubmit:
    def test_happy_path_returns_202(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/time-to-action-submit",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": 8200,
                "action": "approve",
                "reason_tag": "PRICE_WITHIN_TOLERANCE",
            },
        )
        assert r.status_code == 202
        assert r.json() == {"ok": True}

    def test_reason_tag_optional(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/time-to-action-submit",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": 3000,
                "action": "reanalyze",
            },
        )
        assert r.status_code == 202

    def test_records_into_counter(self, client, analyst_token):
        initial = metrics_mod._cases_v2_ttas_count
        initial_tagged = metrics_mod._cases_v2_ttas_with_reason_tag
        r = client.post(
            "/api/v1/metrics/cases/time-to-action-submit",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": 1200,
                "action": "reject",
                "reason_tag": "DISPUTED_TERMS",
            },
        )
        assert r.status_code == 202
        assert metrics_mod._cases_v2_ttas_count == initial + 1
        assert metrics_mod._cases_v2_ttas_by_action["reject"] >= 1
        assert metrics_mod._cases_v2_ttas_with_reason_tag == initial_tagged + 1

    @pytest.mark.parametrize(
        "action,expected_status",
        [
            ("approve", 202),
            ("reject", 202),
            ("reanalyze", 202),
            # override / escalate are one-step actions that report
            # through their own channels — not the comment-dialog set.
            ("override", 422),
            ("escalate", 422),
            ("APPROVE", 422),  # case-sensitive
        ],
    )
    def test_action_vocab(
        self, client, analyst_token, action, expected_status,
    ):
        r = client.post(
            "/api/v1/metrics/cases/time-to-action-submit",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": 500,
                "action": action,
            },
        )
        assert r.status_code == expected_status

    def test_negative_dwell_rejected(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/time-to-action-submit",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "dwell_ms": -1,
                "action": "approve",
            },
        )
        assert r.status_code == 422

    def test_viewer_forbidden(self, client, viewer_token):
        r = client.post(
            "/api/v1/metrics/cases/time-to-action-submit",
            headers=_auth(viewer_token),
            json={"case_id": "case-1", "dwell_ms": 100, "action": "approve"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Analysis-scroll-depth endpoint
# ---------------------------------------------------------------------------


class TestAnalysisScrollDepth:
    def test_happy_path_returns_202(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/analysis-scroll-depth",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "max_scroll_pct": 0.42,
                "acted": True,
            },
        )
        assert r.status_code == 202

    def test_records_into_counter(self, client, analyst_token):
        initial = metrics_mod._cases_v2_scroll_count
        initial_acted = metrics_mod._cases_v2_scroll_acted
        r = client.post(
            "/api/v1/metrics/cases/analysis-scroll-depth",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "max_scroll_pct": 0.9,
                "acted": True,
            },
        )
        assert r.status_code == 202
        assert metrics_mod._cases_v2_scroll_count == initial + 1
        assert metrics_mod._cases_v2_scroll_acted == initial_acted + 1

    def test_out_of_range_pct_rejected(self, client, analyst_token):
        r = client.post(
            "/api/v1/metrics/cases/analysis-scroll-depth",
            headers=_auth(analyst_token),
            json={
                "case_id": "case-1",
                "max_scroll_pct": 1.5,
                "acted": False,
            },
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Prometheus rendering
# ---------------------------------------------------------------------------


def test_metrics_endpoint_exposes_v2_telemetry_counters(
    client, analyst_token,
):
    """The /metrics text must include the three V2 counter classes
    so dashboards can graph them alongside reviewer-activity."""
    # Seed some data so the counters render with non-zero values.
    client.post(
        "/api/v1/metrics/cases/row-click",
        headers=_auth(analyst_token),
        json={"case_id": "c1", "audit_verdict_color": "A"},
    )
    client.post(
        "/api/v1/metrics/cases/time-to-first-action",
        headers=_auth(analyst_token),
        json={"case_id": "c1", "dwell_ms": 2000, "first_action": "approve"},
    )
    client.post(
        "/api/v1/metrics/cases/analysis-scroll-depth",
        headers=_auth(analyst_token),
        json={"case_id": "c1", "max_scroll_pct": 0.5, "acted": True},
    )
    client.post(
        "/api/v1/metrics/cases/time-to-action-submit",
        headers=_auth(analyst_token),
        json={"case_id": "c1", "dwell_ms": 6000, "action": "approve"},
    )
    r = client.get("/api/v1/metrics", headers=_auth(analyst_token))
    assert r.status_code == 200
    body = r.text
    assert "cases_v2_row_clicks_total" in body
    assert "cases_v2_time_to_first_action_total" in body
    assert "cases_v2_time_to_action_submit_total" in body
    assert "cases_v2_analysis_scroll_depth_total" in body
