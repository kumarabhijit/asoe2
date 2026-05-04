"""Tests for the three-tier human intervention model.

Validates the three HITL tiers:
  GREEN  → POST /exceptions/{id}/challenge (analyst+) — RESOLVED → ESCALATED
  YELLOW → PATCH /exceptions/{id}/override (manager+) — PENDING_REVIEW → RESOLVED
  RED    → POST /exceptions/{id}/admin-release (admin) — BLOCKED → PENDING_ADMIN_REVIEW

Also tests:
  - State-machine guards (wrong-state → 409)
  - RBAC enforcement (wrong-role → 403)
  - Action validation (invalid action → 422)
  - SOX audit logging (policy_audit_log entries)
  - Lifecycle state coverage (PENDING_ADMIN_REVIEW in health)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    exception_store.clear()
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


def _sample_event() -> dict:
    return {
        "order_id": "PO-001",
        "po_price": 100.0,
        "sap_base_price": 120.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
    }


def _duplicate_po_event() -> dict:
    return {
        "order_id": "PO-DUP-001",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_resolved(client, token) -> str:
    """Create a RESOLVED exception (GREEN path).

    Uses a small price discrepancy (5%) that passes all thresholds
    and completes the full pipeline → COMPLETE → lifecycle RESOLVED.
    """
    r = client.post(
        "/api/v1/exceptions/resolve",
        json={
            "order_id": "PO-GREEN-001",
            "po_price": 95.0,
            "sap_base_price": 100.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["final_status"] == "COMPLETE", f"Expected COMPLETE, got {data['final_status']}"
    return data["exception_id"]


def _create_pending_review(client, token) -> str:
    """Create a PENDING_REVIEW exception (YELLOW path via explain mode)."""
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json=_sample_event(),
        headers=_auth(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"
    return data["exception_id"]


def _create_blocked(client, token) -> str:
    """Create a BLOCKED exception (RED path — high-confidence DUPLICATE_PO)."""
    r = client.post(
        "/api/v1/exceptions/resolve",
        json=_duplicate_po_event(),
        headers=_auth(token),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["final_status"] == "BLOCKED"
    return data["exception_id"]


# ===========================================================================
# GREEN TIER: Post-Execution Challenge
# ===========================================================================

class TestGreenChallenge:
    """POST /api/v1/exceptions/{id}/challenge — RESOLVED → ESCALATED."""

    def test_challenge_resolved_exception(self, client, analyst_token):
        """Analyst challenges a GREEN-resolved exception for review."""
        exc_id = _create_resolved(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/challenge",
            json={"challenge_reason": "Price adjustment looks incorrect for this retailer"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["lifecycle_state"] == "ESCALATED"
        assert "CHALLENGED:" in data["resolution_notes"]

    def test_challenge_wrong_state_rejected(self, client, analyst_token):
        """Cannot challenge a PENDING_REVIEW exception (wrong state)."""
        exc_id = _create_pending_review(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/challenge",
            json={"challenge_reason": "test"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"

    def test_challenge_blocked_rejected(self, client, analyst_token):
        """Cannot challenge a BLOCKED exception."""
        exc_id = _create_blocked(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/challenge",
            json={"challenge_reason": "test"},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 409

    def test_viewer_cannot_challenge(self, client, analyst_token, viewer_token):
        """Viewers cannot challenge exceptions."""
        exc_id = _create_resolved(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/challenge",
            json={"challenge_reason": "test"},
            headers=_auth(viewer_token),
        )
        assert r.status_code == 403

    def test_challenge_creates_audit_event(self, client, analyst_token):
        """Challenge creates an immutable SOX audit entry."""
        exc_id = _create_resolved(client, analyst_token)
        client.post(
            f"/api/v1/exceptions/{exc_id}/challenge",
            json={"challenge_reason": "Suspicious price delta"},
            headers=_auth(analyst_token),
        )
        audit_log = exception_store.get_audit_log("tenant-a")
        challenge_events = [e for e in audit_log if e["policy_key"] == "EXCEPTION_CHALLENGE"]
        assert len(challenge_events) >= 1
        event = challenge_events[-1]
        assert event["change_reason"] == "Suspicious price delta"


# ===========================================================================
# YELLOW TIER: Override Agent Recommendation
# ===========================================================================

class TestYellowOverride:
    """PATCH /api/v1/exceptions/{id}/disposition — PENDING_REVIEW → RESOLVED."""

    def test_override_with_valid_action(self, client, analyst_token, manager_token):
        """Manager overrides agent recommendation with a valid action."""
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Verified with buyer — both POs are intentional",
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["resolved_action"] == "ALLOW_BOTH"
        # Auditor identity is always derived from user.sub (test-user)
        assert data["resolved_by"] == "test-user"
        assert data["lifecycle_state"] == "RESOLVED"

    def test_override_invalid_action_rejected(self, client, analyst_token, manager_token):
        """Invalid action value is rejected (422)."""
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "YOLO",
                "notes": "test",
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_ACTION"

    def test_override_on_resolved_accepted(self, client, analyst_token, manager_token):
        """Option A: manager can Override a GREEN+RESOLVED exception."""
        exc_id = _create_resolved(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Reviewed post-execution — action refined",
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_override_on_blocked_accepted(self, client, analyst_token, manager_token):
        """Option A: manager can Override a RED+BLOCKED exception directly."""
        exc_id = _create_blocked(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Red verdict overridden with risk acknowledged",
                # ADR-033 §C.2 — DUPLICATE_PO uses curated SCREAMING_SNAKE_CASE codes.
                "reason_tag": "OTHER",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_override_requires_notes(self, client, analyst_token, manager_token):
        """Notes field is mandatory (SOX)."""
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                # notes missing — should fail validation
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 422

    def test_analyst_cannot_override(self, client, analyst_token):
        """Analyst cannot override (manager+ only)."""
        exc_id = _create_pending_review(client, analyst_token)
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "notes": "test",
                "reason_tag": "other",
            },
            headers=_auth(analyst_token),
        )
        assert r.status_code == 403

    def test_override_creates_audit_event(self, client, analyst_token, manager_token):
        """Override creates an immutable SOX audit entry."""
        exc_id = _create_pending_review(client, analyst_token)
        client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "SUPERSEDE",
                "notes": "Original PO superseded by revision",
                "reason_tag": "other",
            },
            headers=_auth(manager_token),
        )
        audit_log = exception_store.get_audit_log("tenant-a")
        # Phase 3: /disposition emits EXCEPTION_RESOLVED with sub_type on
        # new_value (APPROVE/REJECT/OVERRIDE) — one event stream replaces
        # the previous per-endpoint policy_keys.
        override_events = [
            e for e in audit_log
            if e["policy_key"] == "EXCEPTION_RESOLVED"
            and e["new_value"].get("sub_type") in {"APPROVE", "OVERRIDE"}
        ]
        assert len(override_events) >= 1
        event = override_events[-1]
        assert event["change_reason"] == "Original PO superseded by revision"

    def test_all_six_resolution_actions_accepted(self, client, analyst_token, manager_token):
        """All AllowedResolutionAction values are accepted by override."""
        valid_actions = [
            "BLOCK_AND_NOTIFY", "MERGE", "SUPERSEDE",
            "ALLOW_BOTH", "ESCALATE", "REQUEST_BUYER_CONFIRMATION",
        ]
        for action in valid_actions:
            exception_store.clear()
            exc_id = _create_pending_review(client, analyst_token)
            r = client.patch(
                f"/api/v1/exceptions/{exc_id}/disposition",
                json={
                    "action": action,
                    "notes": f"Testing {action}",
                    "reason_tag": "other",
                },
                headers=_auth(manager_token),
            )
            assert r.status_code == 200, f"Action {action} should be accepted"


# ===========================================================================
# RED TIER: Admin Release
# ===========================================================================

class TestRedAdminRelease:
    """POST /api/v1/exceptions/{id}/admin-release — BLOCKED → PENDING_ADMIN_REVIEW."""

    def test_admin_release_blocked_exception(self, client, analyst_token, admin_token):
        """Admin releases a RED-blocked exception for review."""
        exc_id = _create_blocked(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={
                "release_reason": "False positive — retailer threshold incorrect",
                "risk_acknowledgment": True,
            },
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["lifecycle_state"] == "PENDING_ADMIN_REVIEW"
        assert "ADMIN_RELEASE:" in data["resolution_notes"]
        # Original shadow verdict preserved
        assert data["shadow_verdict"] == "GREEN"  # DuplicatePO gets GREEN shadow

    def test_admin_release_requires_risk_acknowledgment(self, client, analyst_token, admin_token):
        """risk_acknowledgment must be True."""
        exc_id = _create_blocked(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={
                "release_reason": "test",
                "risk_acknowledgment": False,
            },
            headers=_auth(admin_token),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "RISK_NOT_ACKNOWLEDGED"

    def test_admin_release_wrong_state_rejected(self, client, analyst_token, admin_token):
        """Cannot admin-release a RESOLVED exception."""
        exc_id = _create_resolved(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={"release_reason": "test", "risk_acknowledgment": True},
            headers=_auth(admin_token),
        )
        assert r.status_code == 409

    def test_manager_cannot_admin_release(self, client, analyst_token, manager_token):
        """Only admin can release RED-blocked exceptions."""
        exc_id = _create_blocked(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={"release_reason": "test", "risk_acknowledgment": True},
            headers=_auth(manager_token),
        )
        assert r.status_code == 403

    def test_analyst_cannot_admin_release(self, client, analyst_token):
        """Analyst cannot release RED-blocked exceptions."""
        exc_id = _create_blocked(client, analyst_token)
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={"release_reason": "test", "risk_acknowledgment": True},
            headers=_auth(analyst_token),
        )
        assert r.status_code == 403

    def test_admin_release_creates_audit_event(self, client, analyst_token, admin_token):
        """Admin release creates an immutable SOX audit entry."""
        exc_id = _create_blocked(client, analyst_token)
        client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={
                "release_reason": "Threshold misconfigured for this retailer",
                "risk_acknowledgment": True,
            },
            headers=_auth(admin_token),
        )
        audit_log = exception_store.get_audit_log("tenant-a")
        release_events = [e for e in audit_log if e["policy_key"] == "EXCEPTION_ADMIN_RELEASE"]
        assert len(release_events) >= 1
        event = release_events[-1]
        assert event["change_reason"] == "Threshold misconfigured for this retailer"

    def test_admin_release_then_approve(self, client, analyst_token, admin_token):
        """Full RED flow: admin-release → approve → EXECUTING."""
        exc_id = _create_blocked(client, analyst_token)

        # Step 1: Admin releases
        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={
                "release_reason": "False positive",
                "risk_acknowledgment": True,
            },
            headers=_auth(admin_token),
        )
        assert r.json()["lifecycle_state"] == "PENDING_ADMIN_REVIEW"

        # Step 2: Admin approves from PENDING_ADMIN_REVIEW
        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "reason_tag": "OTHER", "notes": "Proceeding after admin review"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"

    def test_admin_release_then_reject(self, client, analyst_token, admin_token):
        """Full RED flow: admin-release → reject → REJECTED."""
        exc_id = _create_blocked(client, analyst_token)

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={"release_reason": "Investigating", "risk_acknowledgment": True},
            headers=_auth(admin_token),
        )
        assert r.json()["lifecycle_state"] == "PENDING_ADMIN_REVIEW"

        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "NO_ACTION", "reason_tag": "OTHER", "notes": "After review, RED verdict was correct"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "REJECTED"

    def test_admin_release_then_override(self, client, analyst_token, admin_token):
        """Full RED flow: admin-release → override with specific action."""
        exc_id = _create_blocked(client, analyst_token)

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/admin-release",
            json={"release_reason": "False positive", "risk_acknowledgment": True},
            headers=_auth(admin_token),
        )
        assert r.json()["lifecycle_state"] == "PENDING_ADMIN_REVIEW"

        # PENDING_ADMIN_REVIEW is not in HITL_OVERRIDE_STATES — admin must approve first
        # or we could use override from PENDING_ADMIN_REVIEW if we add it.
        # For now, admin approves then the exception enters EXECUTING.
        r = client.patch(f"/api/v1/exceptions/{exc_id}/disposition",
            json={"action": "ALLOW_BOTH", "reason_tag": "OTHER", "notes": "Releasing with admin approval"},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "RESOLVED"


# ===========================================================================
# PENDING_ADMIN_REVIEW lifecycle state in health endpoint
# ===========================================================================

class TestPendingAdminReviewInHealth:
    def test_pending_admin_review_in_lifecycle_states(self, client):
        """PENDING_ADMIN_REVIEW appears in health endpoint for frontend filters."""
        r = client.get("/api/v1/health")
        states = r.json()["lifecycle_states"]
        assert "PENDING_ADMIN_REVIEW" in states

    def test_lifecycle_states_count_is_12(self, client):
        """12 lifecycle states after Phase 3 dropped EXECUTING."""
        r = client.get("/api/v1/health")
        assert len(r.json()["lifecycle_states"]) == 12
        assert "EXECUTING" not in r.json()["lifecycle_states"]
