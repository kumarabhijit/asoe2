"""Phase 1 unified action model — Option A tests.

Covers:
  - Override eligibility matrix (verdict x role x lifecycle):
      manager on GREEN+RESOLVED           → 200
      manager on YELLOW+PENDING_REVIEW    → 200
      manager on RED+BLOCKED              → 200
      manager on FAILED lifecycle         → 409 INVALID_VERDICT
      analyst on YELLOW+PENDING_REVIEW    → 403
      viewer                              → 403
  - Trust-boundary fix: OverrideRequest no longer accepts ``resolved_by``.
  - Audit ``previous_value.recommended_action`` populated from
    record.resolution_data.
  - Idempotency-Key behavior on /override:
      repeat with same body   → cached response, no second DB write
      repeat with diff body   → 409 IDEMPOTENCY_CONFLICT
  - Dedicated /escalate endpoint:
      analyst on PENDING_REVIEW → 200, lifecycle=ESCALATED, SOX audit
      viewer                    → 403
      already-ESCALATED         → 409
      missing ``reason``        → 422
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token  # re-exported here for use inside test classes
from api.routes.exceptions import _clear_idempotency_cache
from api.store import exception_store
# Re-export so the SoD tests below can mint per-user tokens without needing
# to parameterize fixtures with different subs.
__all__ = ["create_test_token"]


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_three_tier_hitl.py style
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    exception_store.clear()
    _clear_idempotency_cache()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def analyst_token():
    return create_test_token(roles=["analyst"], org="tenant-a")


@pytest.fixture()
def manager_token():
    return create_test_token(roles=["manager"], org="tenant-a")


@pytest.fixture()
def admin_token():
    return create_test_token(roles=["admin"], org="tenant-a")


@pytest.fixture()
def viewer_token():
    return create_test_token(roles=["viewer"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _duplicate_po_event() -> dict:
    return {
        "order_id": "PO-DUP-100",
        "line_item": 1,
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_DUPLICATE_PO",
        "retailer_id": "R-10",
        "line_count": 1,
        "metadata": {
            "signal_scores": {
                "po_number": 1.0, "customer_id": 1.0, "line_items": 0.95,
                "amount": 0.90, "timestamp": 0.80, "ship_to": 0.80,
                "channel": 1.0, "delivery_date": 0.80,
            },
            "matched_po_id": "PO-DUP-000",
        },
    }


def _price_event() -> dict:
    return {
        "order_id": "PO-001",
        "po_price": 100.0,
        "sap_base_price": 120.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
    }


def _create_resolved(client, token) -> str:
    """GREEN → RESOLVED."""
    r = client.post(
        "/api/v1/exceptions/resolve",
        json={
            "order_id": "PO-GREEN-1",
            "po_price": 95.0,
            "sap_base_price": 100.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["final_status"] == "COMPLETE"
    return r.json()["exception_id"]


def _create_pending_review(client, token) -> str:
    """YELLOW → PENDING_REVIEW (via explain mode)."""
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json=_price_event(),
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["final_status"] == "MANUAL_REVIEW_REQUIRED"
    return r.json()["exception_id"]


def _create_blocked(client, token) -> str:
    """RED → BLOCKED (high-confidence duplicate PO)."""
    r = client.post(
        "/api/v1/exceptions/resolve",
        json=_duplicate_po_event(),
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["final_status"] == "BLOCKED"
    return r.json()["exception_id"]


def _force_failed_lifecycle(exception_id: str) -> None:
    """Force the in-memory record into a FAILED-lifecycle/no-verdict shape.

    Simulates a crashed pre-shadow run where there is no agent decision
    to override. Uses the store.update() pass-through because
    shadow_verdict is settable on the record.
    """
    exception_store.update(
        exception_id,
        "tenant-a",
        lifecycle_state="FAILED",
        final_status="FAIL_TO_HUMAN",
        shadow_verdict=None,
    )


# ===========================================================================
# Override eligibility matrix
# ===========================================================================

class TestOverrideEligibilityMatrix:
    def test_manager_on_green_resolved_accepted(self, client, analyst_token, manager_token):
        exc_id = _create_resolved(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "post-exec refinement"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_manager_on_yellow_pending_review_accepted(
        self, client, analyst_token, manager_token,
    ):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "buyer confirmed"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_manager_on_red_blocked_accepted(
        self, client, analyst_token, manager_token,
    ):
        exc_id = _create_blocked(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "risk acknowledged"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_manager_on_failed_rejected_invalid_verdict(
        self, client, analyst_token, manager_token,
    ):
        exc_id = _create_pending_review(client, analyst_token)
        _force_failed_lifecycle(exc_id)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "should fail"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_VERDICT"

    def test_analyst_on_yellow_forbidden(self, client, analyst_token):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "should fail"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 403

    def test_viewer_forbidden(self, client, analyst_token, viewer_token):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "should fail"},
            headers=_auth(viewer_token),
        )
        assert r.status_code == 403


# ===========================================================================
# Trust-boundary fix: resolved_by is not client-settable
# ===========================================================================

class TestResolvedByTrustBoundary:
    def test_resolved_by_field_rejected_by_pydantic(
        self, client, analyst_token, manager_token,
    ):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "trying to spoof identity",
                "resolved_by": "attacker@example.com",
            },
            headers=_auth(manager_token),
        )
        # Pydantic extra="forbid" → 422 validation error
        assert r.status_code == 422


# ===========================================================================
# Audit before-value: recommended_action populated from resolution_data
# ===========================================================================

class TestAuditRecommendedAction:
    def test_recommended_action_populated_in_audit_previous_value(
        self, client, analyst_token, manager_token,
    ):
        """For RED+BLOCKED the recipe runs and stores recommended_action.

        DuplicatePORecipe writes recommended_action into execution_log.outputs
        which _persist_exception copies into record.resolution_data. The
        override audit log's previous_value must surface that value.
        """
        exc_id = _create_blocked(client, analyst_token)

        # Sanity check — the record has a recommended_action to carry through.
        rec = exception_store.get(exc_id, "tenant-a")
        assert rec is not None
        assert rec.resolution_data.get("recommended_action") is not None
        expected_prior_action = rec.resolution_data["recommended_action"]

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "override block"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200

        audit = exception_store.get_audit_log("tenant-a")
        overrides = [e for e in audit if e["policy_key"] == "EXCEPTION_OVERRIDE"]
        assert overrides, "override audit event not emitted"
        prev = overrides[-1]["previous_value"]
        assert prev["recommended_action"] == expected_prior_action
        assert prev["lifecycle_state"] == "BLOCKED"


# ===========================================================================
# Idempotency-Key on /override
# ===========================================================================

class TestOverrideIdempotency:
    def test_repeat_same_key_same_body_returns_cached(
        self, client, analyst_token, manager_token,
    ):
        exc_id = _create_pending_review(client, analyst_token)
        body = {"action": "ALLOW_BOTH", "notes": "first pass"}
        headers = {**_auth(manager_token), "Idempotency-Key": "abc_123-XYZ"}

        r1 = client.patch(
            f"/api/v1/exceptions/{exc_id}/override", json=body, headers=headers,
        )
        assert r1.status_code == 200
        first_updated_at = r1.json()["updated_at"]

        # Count audit events after first call.
        audit_before = len([
            e for e in exception_store.get_audit_log("tenant-a")
            if e["policy_key"] == "EXCEPTION_OVERRIDE"
        ])

        r2 = client.patch(
            f"/api/v1/exceptions/{exc_id}/override", json=body, headers=headers,
        )
        assert r2.status_code == 200
        # Cached response returns verbatim — identical updated_at proves no
        # second DB write occurred.
        assert r2.json()["updated_at"] == first_updated_at

        audit_after = len([
            e for e in exception_store.get_audit_log("tenant-a")
            if e["policy_key"] == "EXCEPTION_OVERRIDE"
        ])
        assert audit_after == audit_before, "audit log must not grow on cache hit"

    def test_same_key_different_body_conflict(
        self, client, analyst_token, manager_token,
    ):
        exc_id = _create_pending_review(client, analyst_token)
        headers = {**_auth(manager_token), "Idempotency-Key": "conflict-key-1"}

        r1 = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "first"},
            headers=headers,
        )
        assert r1.status_code == 200

        r2 = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={"action": "SUPERSEDE", "notes": "different body"},
            headers=headers,
        )
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ===========================================================================
# /escalate endpoint
# ===========================================================================

class TestEscalateEndpoint:
    def test_analyst_can_escalate_pending_review(self, client, analyst_token):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/escalate",
            json={"reason": "SOX: routing to manager for sign-off"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["lifecycle_state"] == "ESCALATED"

        audit = exception_store.get_audit_log("tenant-a")
        events = [e for e in audit if e["policy_key"] == "EXCEPTION_ESCALATED"]
        assert len(events) == 1
        event = events[0]
        assert event["change_reason"] == "SOX: routing to manager for sign-off"
        assert event["previous_value"]["lifecycle_state"] == "PENDING_REVIEW"
        assert event["new_value"]["lifecycle_state"] == "ESCALATED"
        assert event["new_value"]["reason"] == "SOX: routing to manager for sign-off"

    def test_viewer_cannot_escalate(self, client, analyst_token, viewer_token):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/escalate",
            json={"reason": "should not be permitted"},
            headers=_auth(viewer_token),
        )
        assert r.status_code == 403

    def test_already_escalated_returns_409(self, client, analyst_token):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/escalate",
            json={"reason": "first escalation"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/escalate",
            json={"reason": "second escalation should fail"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"

    def test_escalate_without_reason_returns_422(self, client, analyst_token):
        exc_id = _create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/escalate",
            json={},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 422

    def test_escalate_preserves_resolution_data(self, client, analyst_token):
        """Escalate must append to resolution_data.escalations, not overwrite."""
        exc_id = _create_blocked(client, analyst_token)
        rec_before = exception_store.get(exc_id, "tenant-a")
        assert rec_before is not None
        prior_action = rec_before.resolution_data.get("recommended_action")

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/escalate",
            json={"reason": "needs manager review", "to_role": "manager"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        # Prior recipe output preserved
        assert data["resolution_data"].get("recommended_action") == prior_action
        # Escalation metadata appended
        escalations = data["resolution_data"].get("escalations") or []
        assert len(escalations) == 1
        assert escalations[0]["reason"] == "needs manager review"
        assert escalations[0]["to_role"] == "manager"
        assert escalations[0]["from_lifecycle_state"] == "BLOCKED"


# ---------------------------------------------------------------------------
# Phase 2 — Segregation of Duties
# ---------------------------------------------------------------------------


class TestSegregationOfDuties:
    """SoD: the user who previously resolved an exception must not be the
    same user overriding it. Protects against silent self-approval of an
    alternate resolution."""

    def test_same_user_cannot_override_own_resolution(self, client, analyst_token):
        # Priya (manager) resolves first, then tries to override herself
        priya = create_test_token(sub="priya@x", roles=["manager"], org="tenant-a")
        exception_id = _create_pending_review(client, analyst_token)
        r1 = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "first resolution",
                "reason_tag": "policy_exception",
            },
            headers=_auth(priya),
        )
        assert r1.status_code == 200
        r2 = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={
                "action": "SUPERSEDE",
                "notes": "trying to self-revise",
                "reason_tag": "other",
            },
            headers=_auth(priya),
        )
        assert r2.status_code == 403
        assert r2.json()["error"]["code"] == "SOD_VIOLATION"

    def test_different_user_can_override_prior_resolution(self, client, analyst_token):
        # Priya resolves; Raj (admin, different sub) overrides → 200
        priya = create_test_token(sub="priya@x", roles=["manager"], org="tenant-a")
        raj = create_test_token(sub="raj@x", roles=["admin"], org="tenant-a")
        exception_id = _create_pending_review(client, analyst_token)
        r1 = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "first pass",
                "reason_tag": "data_error",
            },
            headers=_auth(priya),
        )
        assert r1.status_code == 200
        r2 = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={
                "action": "SUPERSEDE",
                "notes": "admin correction",
                "reason_tag": "customer_concession",
            },
            headers=_auth(raj),
        )
        assert r2.status_code == 200, r2.json()


# ---------------------------------------------------------------------------
# Phase 2 — reason_tag controlled vocabulary
# ---------------------------------------------------------------------------


class TestReasonTagVocabulary:

    def test_valid_reason_tag_accepted(self, client, analyst_token, manager_token):
        exception_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "buyer confirmed concession",
                "reason_tag": "customer_concession",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200

    def test_invalid_reason_tag_rejected(self, client, analyst_token, manager_token):
        exception_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "should fail",
                "reason_tag": "made_up_tag",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_REASON_TAG"

    def test_omitted_reason_tag_defaults_to_other(
        self, client, analyst_token, manager_token
    ):
        """Phase 2 compatibility: callers without reason_tag default to 'other'.
        Phase 3 will tighten this to required."""
        exception_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={"action": "ALLOW_BOTH", "notes": "legacy caller"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200

    def test_reason_tag_persisted_to_audit(
        self, client, analyst_token, manager_token
    ):
        from api.store import exception_store
        exception_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exception_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "recipe misclassified intent",
                "reason_tag": "agent_misclassification",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        audit = exception_store.get_audit_log("tenant-a")
        override_events = [e for e in audit if e["policy_key"] == "EXCEPTION_OVERRIDE"]
        assert override_events, "EXCEPTION_OVERRIDE audit event missing"
        new_value = override_events[0]["new_value"]
        assert new_value.get("reason_tag") == "agent_misclassification"

    def test_health_exposes_reason_tag_vocabulary(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["allowed_override_reason_tags"], list)
        assert "customer_concession" in body["allowed_override_reason_tags"]
        assert "other" in body["allowed_override_reason_tags"]
