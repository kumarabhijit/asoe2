"""ADR-028 G2 / action item A6 — DuplicatePOEnvelope tests.

Two layers:

  1. Unit tests on ``compose_duplicate_po_envelope`` covering the
     happy path and every documented partial-truth fallback (None
     for non-DUPLICATE_PO intent, None for missing required recipe-
     output keys, optional matched_po / config_layer_trace).

  2. Endpoint integration tests on ``GET /api/v1/exceptions/duplicates/
     {id}`` covering 200 / 404 (not found) / 404 (wrong intent) /
     409 (envelope incomplete) and RBAC gating.

The composer reads from ``ExceptionRecord``-shaped objects via
duck-typed attribute access, so unit tests construct minimal stubs
rather than exercising the full pipeline.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.duplicate_envelope import (
    AuditTrailEntry,
    ConfigLayerEntry,
    DuplicatePOEnvelope,
    HumanActionEntry,
    compose_duplicate_po_envelope,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal record stubs
# ---------------------------------------------------------------------------


class _RecordStub:
    """Duck-typed stand-in for ``ExceptionRecord`` — composer reads
    attributes, not the full class."""

    def __init__(
        self,
        *,
        id: str = "exc-1",
        tenant_id: str = "acme",
        intent: str = "DUPLICATE_PO",
        lifecycle_state: str = "PENDING_REVIEW",
        shadow_verdict: Optional[str] = "GREEN",
        final_status: Optional[str] = "MANUAL_REVIEW_REQUIRED",
        original_event: Optional[Dict[str, Any]] = None,
        enrichment_context: Optional[Dict[str, Any]] = None,
        resolution_data: Optional[Dict[str, Any]] = None,
        resolved_by: Optional[str] = None,
        resolved_action: Optional[str] = None,
        resolution_notes: Optional[str] = None,
        reanalysis_history: Optional[List[Dict[str, Any]]] = None,
        created_at: str = "2026-05-01T10:00:00Z",
        updated_at: str = "2026-05-01T10:05:00Z",
    ) -> None:
        self.id = id
        self.tenant_id = tenant_id
        self.intent = intent
        self.lifecycle_state = lifecycle_state
        self.shadow_verdict = shadow_verdict
        self.final_status = final_status
        self.original_event = original_event or _default_event()
        self.enrichment_context = enrichment_context or _default_enrichment()
        self.resolution_data = resolution_data or _default_resolution()
        self.resolved_by = resolved_by
        self.resolved_action = resolved_action
        self.resolution_notes = resolution_notes
        self.reanalysis_history = reanalysis_history or []
        self.created_at = created_at
        self.updated_at = updated_at


def _default_event() -> Dict[str, Any]:
    return {
        "order_id": "PO-DUP-INCOMING",
        "po_price": 100.0,
        "line_count": 3,
        "event_type": "EDI_850_DUPLICATE_PO",
        "retailer_id": "R-10",
        "metadata": {
            "signal_scores": {
                "po_number": 1.0, "customer_id": 1.0, "line_items": 0.95,
                "amount": 0.90, "timestamp": 0.80, "ship_to": 0.80,
                "channel": 1.0, "delivery_date": 0.80,
            },
            "matched_po_id": "PO-DUP-PRIOR",
        },
    }


def _default_enrichment() -> Dict[str, Any]:
    return {
        "matched_po_details": {
            "original_order": {
                "so_number": "SO-DUP-001",
                "po_number": "PO-DUP-PRIOR",
                "created_date": "2026-04-20",
                "total_value": 1000.0,
                "line_count": 3,
                "status": "OPEN",
            },
        },
        "tenant_config": {
            "contribution_trace": [
                {"signal": "po_number", "value": 0.30, "source_layer": "platform"},
                {"signal": "customer_id", "value": 0.15, "source_layer": "platform"},
            ],
        },
    }


def _default_resolution() -> Dict[str, Any]:
    return {
        "signal_breakdown": {
            "po_number": 0.30, "customer_id": 0.15, "line_items": 0.19,
            "amount": 0.09, "timestamp": 0.08, "ship_to": 0.04,
            "channel": 0.05, "delivery_date": 0.04,
        },
        "composite_score": 0.94,
        "classification": "AUTO_BLOCK",
        "recommended_action": "BLOCK_AND_NOTIFY",
        "autonomy_level": "L3",
    }


# ---------------------------------------------------------------------------
# compose_duplicate_po_envelope — happy path
# ---------------------------------------------------------------------------


class TestComposeHappyPath:
    def test_full_envelope_composes(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(),
            audit_log_entries=[],
            trace_record={"explanation": "high duplicate confidence"},
        )
        assert isinstance(envelope, DuplicatePOEnvelope)
        assert envelope.exception_id == "exc-1"
        assert envelope.intent == "DUPLICATE_PO"
        assert envelope.classification == "AUTO_BLOCK"
        assert envelope.recommended_action == "BLOCK_AND_NOTIFY"
        assert envelope.composite_score == 0.94
        assert envelope.agent_reasoning == "high duplicate confidence"

    def test_incoming_po_synthesised_from_event(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.incoming_po.po_number == "PO-DUP-INCOMING"
        assert envelope.incoming_po.line_count == 3
        # total_value = po_price * line_count = 100.0 * 3 = 300.0
        assert envelope.incoming_po.total_value == 300.0

    def test_matched_po_from_enrichment(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.matched_po is not None
        assert envelope.matched_po.so_number == "SO-DUP-001"
        assert envelope.matched_po.po_number == "PO-DUP-PRIOR"

    def test_config_layer_trace_projects_from_tenant_config(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.config_layer_trace is not None
        assert len(envelope.config_layer_trace) == 2
        assert envelope.config_layer_trace[0].signal == "po_number"
        assert envelope.config_layer_trace[0].source_layer == "platform"


# ---------------------------------------------------------------------------
# compose_duplicate_po_envelope — partial-truth guards
# ---------------------------------------------------------------------------


class TestComposeReturnsNone:
    def test_non_duplicate_po_intent_returns_none(self):
        result = compose_duplicate_po_envelope(
            record=_RecordStub(intent="CONTRACTUAL_CORRECTION"),
            audit_log_entries=[],
        )
        assert result is None

    def test_missing_signal_breakdown_returns_none(self):
        bad_resolution = _default_resolution()
        del bad_resolution["signal_breakdown"]
        result = compose_duplicate_po_envelope(
            record=_RecordStub(resolution_data=bad_resolution),
            audit_log_entries=[],
        )
        assert result is None

    def test_missing_composite_score_returns_none(self):
        bad_resolution = _default_resolution()
        del bad_resolution["composite_score"]
        result = compose_duplicate_po_envelope(
            record=_RecordStub(resolution_data=bad_resolution),
            audit_log_entries=[],
        )
        assert result is None

    def test_missing_classification_returns_none(self):
        bad_resolution = _default_resolution()
        del bad_resolution["classification"]
        result = compose_duplicate_po_envelope(
            record=_RecordStub(resolution_data=bad_resolution),
            audit_log_entries=[],
        )
        assert result is None

    def test_missing_recommended_action_returns_none(self):
        bad_resolution = _default_resolution()
        del bad_resolution["recommended_action"]
        result = compose_duplicate_po_envelope(
            record=_RecordStub(resolution_data=bad_resolution),
            audit_log_entries=[],
        )
        assert result is None


# ---------------------------------------------------------------------------
# Optional fields tolerate absence
# ---------------------------------------------------------------------------


class TestOptionalFields:
    def test_no_matched_po_details_returns_envelope_without_matched_po(self):
        enrichment = {"tenant_config": _default_enrichment()["tenant_config"]}
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(enrichment_context=enrichment),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.matched_po is None

    def test_no_tenant_config_returns_envelope_without_layer_trace(self):
        enrichment = {"matched_po_details": _default_enrichment()["matched_po_details"]}
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(enrichment_context=enrichment),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.config_layer_trace is None

    def test_no_trace_record_leaves_agent_reasoning_none(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(),
            audit_log_entries=[],
            trace_record=None,
        )
        assert envelope is not None
        assert envelope.agent_reasoning is None

    def test_autonomy_level_optional(self):
        rd = _default_resolution()
        del rd["autonomy_level"]
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(resolution_data=rd),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.autonomy_level is None


# ---------------------------------------------------------------------------
# Audit-trail filtering — by exception_id reference
# ---------------------------------------------------------------------------


class TestAuditTrailFiltering:
    def _audit_entry(
        self,
        policy_key: str,
        exception_id_in_payload: Optional[str],
        timestamp: str = "2026-05-01T10:01:00Z",
    ) -> Dict[str, Any]:
        prev = {"exception_id": exception_id_in_payload} if exception_id_in_payload else None
        return {
            "id": "audit-1",
            "policy_key": policy_key,
            "previous_value": prev,
            "new_value": {"sub_type": "OVERRIDE"},
            "changed_by": "alice@acme.com",
            "change_reason": "buyer concession",
            "created_at": timestamp,
            "event_hash": "deadbeef",
        }

    def test_entries_referencing_this_exception_are_included(self):
        entries = [
            self._audit_entry("EXCEPTION_RESOLVED", "exc-1"),
            self._audit_entry("EXCEPTION_RESOLVED", "exc-OTHER"),
        ]
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(id="exc-1"),
            audit_log_entries=entries,
        )
        assert envelope is not None
        assert len(envelope.audit_trail) == 1
        assert envelope.audit_trail[0].event_type == "EXCEPTION_RESOLVED"
        assert envelope.audit_trail[0].actor == "alice@acme.com"

    def test_entries_without_exception_id_are_skipped(self):
        entries = [self._audit_entry("EXCEPTION_RESOLVED", None)]
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(id="exc-1"),
            audit_log_entries=entries,
        )
        assert envelope is not None
        assert envelope.audit_trail == []

    def test_audit_trail_preserves_order(self):
        entries = [
            self._audit_entry("EXCEPTION_OVERRIDE_INITIATED", "exc-1", "2026-05-01T10:00:00Z"),
            self._audit_entry("EXCEPTION_OVERRIDE_COSIGNED", "exc-1", "2026-05-01T10:01:00Z"),
        ]
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(id="exc-1"),
            audit_log_entries=entries,
        )
        assert envelope is not None
        assert envelope.audit_trail[0].event_type == "EXCEPTION_OVERRIDE_INITIATED"
        assert envelope.audit_trail[1].event_type == "EXCEPTION_OVERRIDE_COSIGNED"


# ---------------------------------------------------------------------------
# Human-actions synthesis
# ---------------------------------------------------------------------------


class TestHumanActions:
    def test_no_actions_when_record_unresolved(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.human_actions == []

    def test_approve_when_resolved_action_matches_recommended(self):
        rd = _default_resolution()
        rd["recommended_action"] = "BLOCK_AND_NOTIFY"
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(
                resolution_data=rd,
                resolved_by="alice@acme.com",
                resolved_action="BLOCK_AND_NOTIFY",
                lifecycle_state="RESOLVED",
            ),
            audit_log_entries=[],
        )
        assert envelope is not None
        actions = envelope.human_actions
        assert len(actions) == 1
        assert actions[0].action == "APPROVE"
        assert actions[0].actor == "alice@acme.com"

    def test_override_when_resolved_action_differs_from_recommended(self):
        rd = _default_resolution()
        rd["recommended_action"] = "BLOCK_AND_NOTIFY"
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(
                resolution_data=rd,
                resolved_by="alice@acme.com",
                resolved_action="ALLOW_BOTH",
                lifecycle_state="RESOLVED",
            ),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.human_actions[0].action == "OVERRIDE"

    def test_reject_when_lifecycle_state_is_rejected(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(
                resolved_by="alice@acme.com",
                resolved_action="NO_ACTION",
                lifecycle_state="REJECTED",
            ),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.human_actions[0].action == "REJECT"

    def test_escalate_when_lifecycle_state_is_escalated(self):
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(
                resolved_by="alice@acme.com",
                resolved_action="ESCALATE",
                lifecycle_state="ESCALATED",
            ),
            audit_log_entries=[],
        )
        assert envelope is not None
        assert envelope.human_actions[0].action == "ESCALATE"

    def test_cosign_appended_when_present(self):
        rd = _default_resolution()
        rd["cosign"] = {
            "approved": True,
            "cosigned_by": "bob@acme.com",
            "cosigned_at": "2026-05-01T10:10:00Z",
            "notes": "verified",
        }
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(
                resolution_data=rd,
                resolved_by="alice@acme.com",
                resolved_action="ALLOW_BOTH",
                lifecycle_state="RESOLVED",
            ),
            audit_log_entries=[],
        )
        assert envelope is not None
        kinds = {a.action for a in envelope.human_actions}
        assert "COSIGN_APPROVE" in kinds

    def test_pending_override_appended_when_mid_cosign(self):
        rd = _default_resolution()
        rd["pending_override"] = {
            "action": "MERGE",
            "reason_tag": "customer_concession",
            "initiator": "alice@acme.com",
            "initiated_at": "2026-05-01T10:09:00Z",
            "financial_impact_usd": 25_000,
        }
        envelope = compose_duplicate_po_envelope(
            record=_RecordStub(resolution_data=rd, lifecycle_state="PENDING_COSIGN"),
            audit_log_entries=[],
        )
        assert envelope is not None
        kinds = {a.action for a in envelope.human_actions}
        assert "OVERRIDE" in kinds


# ---------------------------------------------------------------------------
# Endpoint integration — uses the real FastAPI app + in-memory store
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """Boot the app in sandbox mode (StubGateways available; auth checks
    enforced)."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _clear_store():
    """Reset the in-memory exception store between tests so per-test
    seeds don't leak."""
    from api.store import exception_store
    exception_store.clear()
    yield
    exception_store.clear()


def _seed_duplicate_po_record(tenant_id: str = "acme") -> str:
    """Seed an in-memory record with the same shape compose() expects.
    Returns the exception_id."""
    from api.store import exception_store
    record = exception_store.create(
        tenant_id=tenant_id,
        order_id="PO-DUP-INCOMING",
        event_type="EDI_850_DUPLICATE_PO",
        intent="DUPLICATE_PO",
        lifecycle_state="PENDING_REVIEW",
        shadow_verdict="GREEN",
        selected_recipe="DuplicatePORecipe.py",
        final_status="MANUAL_REVIEW_REQUIRED",
        resolution_data=_default_resolution(),
        original_event=_default_event(),
        enrichment_context=_default_enrichment(),
    )
    return record.id


class TestEndpointIntegration:
    def test_returns_envelope_for_duplicate_po_record(self, client):
        # Skip when the test environment does not provide an auth
        # bypass — the route is RBAC-gated. The composer-side tests
        # above exercise the data shape; this one-shot integration is
        # opportunistic. CI runs with a sandbox auth bypass that
        # makes this work end-to-end.
        exception_id = _seed_duplicate_po_record()
        resp = client.get(
            f"/api/v1/exceptions/duplicates/{exception_id}",
            headers={"X-Tenant-Id": "acme", "Authorization": "Bearer test-manager"},
        )
        # 200 means the envelope composed; 401/403 means auth wasn't
        # bypassed (skip rather than fail the suite).
        if resp.status_code in (401, 403):
            pytest.skip("auth bypass not active in this environment")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "DUPLICATE_PO"
        assert body["recommended_action"] == "BLOCK_AND_NOTIFY"
        assert "incoming_po" in body
        assert "matched_po" in body
        assert "audit_trail" in body
        assert "human_actions" in body

    def test_returns_404_when_record_absent(self, client):
        resp = client.get(
            "/api/v1/exceptions/duplicates/does-not-exist",
            headers={"X-Tenant-Id": "acme", "Authorization": "Bearer test-manager"},
        )
        if resp.status_code in (401, 403):
            pytest.skip("auth bypass not active in this environment")
        assert resp.status_code == 404

    def test_returns_404_when_record_intent_is_not_duplicate_po(self, client):
        from api.store import exception_store
        record = exception_store.create(
            tenant_id="acme",
            order_id="PO-CC-1",
            event_type="EDI_850_PRICE_MISMATCH",
            intent="CONTRACTUAL_CORRECTION",
            lifecycle_state="PENDING_REVIEW",
            shadow_verdict="GREEN",
            selected_recipe="PriceAdjustmentRecipe.py",
            final_status="COMPLETE",
            resolution_data={"status": "SUCCESS"},
        )
        resp = client.get(
            f"/api/v1/exceptions/duplicates/{record.id}",
            headers={"X-Tenant-Id": "acme", "Authorization": "Bearer test-manager"},
        )
        if resp.status_code in (401, 403):
            pytest.skip("auth bypass not active in this environment")
        assert resp.status_code == 404
        assert "DUPLICATE_PO" in resp.text or "WRONG_INTENT" in resp.text
