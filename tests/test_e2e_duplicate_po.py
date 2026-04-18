"""End-to-end data-flow tests for the DUPLICATE_PO use case.

Validates the full API surface that the asoe-ui Exception Queue page
consumes for Duplicate PO exceptions:

  1. Health endpoint serves DUPLICATE_PO in dynamic enums
  2. Resolve endpoint classifies, audits, and executes a Duplicate PO event
  3. Exception CRUD: list (with filters), detail, trace, stats
  4. HITL actions: approve / reject on MANUAL_REVIEW_REQUIRED exceptions
  5. Response shapes match the TypeScript types in asoe-ui/src/types/

Each test asserts on exact field names and value shapes so that any
backend contract change that breaks the frontend is caught here.
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
def tenant_b_token():
    return create_test_token(roles=["analyst"], org="tenant-b")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Sample DUPLICATE_PO events
# ---------------------------------------------------------------------------

def _duplicate_po_high_confidence() -> dict:
    """High-confidence duplicate PO — should auto-resolve (BLOCK_AND_NOTIFY)."""
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
                "po_number": 1.0,
                "customer_id": 1.0,
                "line_items": 0.95,
                "amount": 0.90,
                "timestamp": 0.80,
                "ship_to": 0.80,
                "channel": 1.0,
                "delivery_date": 0.80,
            },
            "matched_po_id": "PO-DUP-000",
        },
    }


def _duplicate_po_medium_confidence() -> dict:
    """Medium-confidence duplicate PO — should route to MANUAL_REVIEW_REQUIRED."""
    return {
        "order_id": "PO-DUP-002",
        "line_item": 1,
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_DUPLICATE_PO",
        "retailer_id": "R-05",
        "line_count": 2,
        "metadata": {
            "signal_scores": {
                "po_number": 0.80,
                "customer_id": 1.0,
                "line_items": 0.70,
                "amount": 0.75,
                "timestamp": 0.30,
                "ship_to": 0.60,
                "channel": 1.0,
                "delivery_date": 0.50,
            },
            "matched_po_id": "PO-DUP-001",
        },
    }


def _duplicate_po_low_confidence() -> dict:
    """Low-confidence duplicate PO — below soft-flag threshold."""
    return {
        "order_id": "PO-DUP-003",
        "line_item": 1,
        "po_price": 8.96,
        "sap_base_price": 8.96,
        "event_type": "EDI_850_DUPLICATE_PO",
        "retailer_id": "R-08",
        "line_count": 1,
        "metadata": {
            "signal_scores": {
                "po_number": 0.0,
                "customer_id": 1.0,
                "line_items": 0.0,
                "amount": 0.0,
                "timestamp": 0.0,
                "ship_to": 0.0,
                "channel": 1.0,
                "delivery_date": 0.0,
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. Health endpoint — DUPLICATE_PO in dynamic enums
# ---------------------------------------------------------------------------

class TestHealthDuplicatePO:
    """The frontend useHealth() hook depends on these exact fields."""

    def test_health_response_shape_for_frontend(self, client):
        """HealthResponse must contain every field asoe-ui/src/types/exceptions.ts expects."""
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        data = r.json()

        # Fields expected by HealthResponse type in asoe-ui
        assert data["status"] == "ok"
        assert isinstance(data["version"], str)
        assert isinstance(data["kill_switch"], bool)
        assert isinstance(data["explain_mode"], bool)
        assert isinstance(data["allowed_intents"], list)
        assert isinstance(data["lifecycle_states"], list)
        assert isinstance(data["allowed_recipes"], list)

    def test_duplicate_po_in_allowed_intents(self, client):
        """DUPLICATE_PO must appear in allowed_intents for the frontend filter dropdown."""
        r = client.get("/api/v1/health")
        intents = r.json()["allowed_intents"]
        assert "DUPLICATE_PO" in intents
        # All four V1 intents
        assert "CONTRACTUAL_CORRECTION" in intents
        assert "CREDIT_BLOCK" in intents
        assert "MASS_PRICING_ERROR" in intents

    def test_duplicate_po_recipe_in_allowed_recipes(self, client):
        """DuplicatePORecipe.py must appear for the frontend to display recipe names."""
        r = client.get("/api/v1/health")
        recipes = r.json()["allowed_recipes"]
        assert "DuplicatePORecipe.py" in recipes

    def test_lifecycle_states_complete(self, client):
        """All 13 lifecycle states must be served — frontend uses these for
        filter dropdowns. PENDING_COSIGN was added in Phase 2 #5 for the
        four-eyes high-value override control."""
        r = client.get("/api/v1/health")
        states = r.json()["lifecycle_states"]
        assert len(states) == 13
        expected = [
            "INGESTED", "CLASSIFYING", "AUDITING", "PENDING_REVIEW",
            "ESCALATED", "PENDING_ADMIN_REVIEW", "PENDING_COSIGN",
            "EXECUTING", "RESOLVED", "FAILED", "BLOCKED", "REJECTED", "CLOSED",
        ]
        for s in expected:
            assert s in states, f"Missing lifecycle state: {s}"


# ---------------------------------------------------------------------------
# 2. Resolve DUPLICATE_PO — full pipeline
# ---------------------------------------------------------------------------

class TestResolveDuplicatePO:
    """POST /api/v1/exceptions/resolve with DUPLICATE_PO events.

    The frontend ExceptionDetailPanel reads every field in ResolveResponse.
    """

    def test_high_confidence_blocks_duplicate(self, client, analyst_token):
        """High signal scores → GREEN shadow → DuplicatePORecipe → BLOCKED.

        High-confidence duplicates are auto-blocked (BLOCK_AND_NOTIFY action
        at autonomy L3). BLOCKED is the correct terminal status — the duplicate
        PO is being blocked from processing.
        """
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_duplicate_po_high_confidence(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        # ResolveResponse field contract
        assert isinstance(data["exception_id"], str)
        assert isinstance(data["trace_id"], str)
        assert data["intent"] == "DUPLICATE_PO"
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "DuplicatePORecipe.py"
        assert data["final_status"] == "BLOCKED"
        assert isinstance(data["explanation"], str)
        assert data["execution_log"] is not None
        assert isinstance(data["effect_results"], list)

    def test_resolve_response_execution_log_fields(self, client, analyst_token):
        """Execution log contains recipe outputs the frontend detail panel displays."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_duplicate_po_high_confidence(),
            headers=_auth(analyst_token),
        )
        data = r.json()
        log = data["execution_log"]
        assert "recipe_name" in log
        assert log["recipe_name"] == "DuplicatePORecipe.py"
        assert "outputs" in log
        assert "trace_id" in log

    def test_resolve_response_has_recommended_action(self, client, analyst_token):
        """Recipe output includes recommended_action used by frontend detail panel."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_duplicate_po_high_confidence(),
            headers=_auth(analyst_token),
        )
        outputs = r.json()["execution_log"]["outputs"]
        assert "recommended_action" in outputs
        allowed_actions = {
            "BLOCK_AND_NOTIFY", "MERGE", "SUPERSEDE",
            "ALLOW_BOTH", "ESCALATE", "REQUEST_BUYER_CONFIRMATION",
        }
        assert outputs["recommended_action"] in allowed_actions

    def test_low_confidence_duplicate_po(self, client, analyst_token):
        """Low signals → DUPLICATE_PO intent still classified, recipe runs."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_duplicate_po_low_confidence(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "DUPLICATE_PO"
        assert data["selected_recipe"] == "DuplicatePORecipe.py"
        assert data["final_status"] is not None

    def test_medium_confidence_routes_to_review(self, client, analyst_token):
        """Medium signals → autonomy L1/L2 → MANUAL_REVIEW_REQUIRED."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_duplicate_po_medium_confidence(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["intent"] == "DUPLICATE_PO"
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "DuplicatePORecipe.py"
        # Medium confidence should require human review based on autonomy level
        assert data["final_status"] in ("MANUAL_REVIEW_REQUIRED", "COMPLETE")


# ---------------------------------------------------------------------------
# 3. Exception CRUD — DUPLICATE_PO lifecycle
# ---------------------------------------------------------------------------

class TestDuplicatePOCrud:
    """Tests the CRUD endpoints the Exception Queue page uses.

    ExceptionListPane calls: GET /exceptions + GET /exceptions/stats
    ExceptionDetailPanel calls: GET /exceptions/{id} + GET /exceptions/{id}/trace
    """

    def _resolve_duplicate_po(self, client, token) -> str:
        """Resolve a DUPLICATE_PO event and return the exception_id."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_duplicate_po_high_confidence(),
            headers=_auth(token),
        )
        return r.json()["exception_id"]

    def test_list_includes_duplicate_po(self, client, analyst_token):
        """GET /exceptions returns DUPLICATE_PO exceptions in the list."""
        exc_id = self._resolve_duplicate_po(client, analyst_token)
        r = client.get("/api/v1/exceptions", headers=_auth(analyst_token))
        assert r.status_code == 200
        data = r.json()

        # PaginatedResponse shape (frontend src/types/api.ts)
        assert "data" in data
        assert "has_more" in data
        assert isinstance(data["data"], list)
        assert len(data["data"]) >= 1

        # ExceptionSummary field contract (frontend src/types/exceptions.ts)
        summary = data["data"][0]
        assert summary["id"] == exc_id
        assert summary["order_id"] == "PO-DUP-001"
        assert summary["event_type"] == "EDI_850_DUPLICATE_PO"
        assert summary["intent"] == "DUPLICATE_PO"
        assert summary["selected_recipe"] == "DuplicatePORecipe.py"
        assert summary["shadow_verdict"] == "GREEN"
        assert isinstance(summary["lifecycle_state"], str)
        assert isinstance(summary["created_at"], str)
        assert isinstance(summary["updated_at"], str)

    def test_list_filter_by_duplicate_po_intent(self, client, analyst_token):
        """Frontend filter dropdown sends ?intent=DUPLICATE_PO."""
        self._resolve_duplicate_po(client, analyst_token)
        # Also create a non-DUPLICATE_PO exception
        client.post(
            "/api/v1/exceptions/resolve",
            json={
                "order_id": "PO-PRICING-001",
                "po_price": 100.0,
                "sap_base_price": 120.0,
                "event_type": "EDI_850_PRICE_MISMATCH",
            },
            headers=_auth(analyst_token),
        )

        # Filter by DUPLICATE_PO
        r = client.get(
            "/api/v1/exceptions?intent=DUPLICATE_PO",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        results = r.json()["data"]
        assert len(results) >= 1
        for exc in results:
            assert exc["intent"] == "DUPLICATE_PO"

    def test_list_filter_by_lifecycle_state(self, client, analyst_token):
        """Frontend filter dropdown sends ?status=RESOLVED."""
        self._resolve_duplicate_po(client, analyst_token)
        r = client.get(
            "/api/v1/exceptions?status=RESOLVED",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        # Should be a valid response regardless of results
        assert "data" in r.json()

    def test_detail_response_shape(self, client, analyst_token):
        """GET /exceptions/{id} must include all fields ExceptionDetailPanel expects."""
        exc_id = self._resolve_duplicate_po(client, analyst_token)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        # ExceptionDetailResponse field contract
        assert data["id"] == exc_id
        assert data["tenant_id"] == "tenant-a"
        assert data["order_id"] == "PO-DUP-001"
        assert data["event_type"] == "EDI_850_DUPLICATE_PO"
        assert data["intent"] == "DUPLICATE_PO"
        assert isinstance(data["lifecycle_state"], str)
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "DuplicatePORecipe.py"
        assert data["final_status"] is not None
        assert isinstance(data["trace_id"], str)
        assert isinstance(data["resolution_data"], dict)
        assert isinstance(data["created_at"], str)
        assert isinstance(data["updated_at"], str)
        # Override fields (nullable)
        assert "resolved_by" in data
        assert "resolved_action" in data
        assert "resolution_notes" in data

    def test_detail_resolution_data_contains_recipe_outputs(self, client, analyst_token):
        """resolution_data must contain recipe outputs for the detail panel."""
        exc_id = self._resolve_duplicate_po(client, analyst_token)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=_auth(analyst_token),
        )
        resolution = r.json()["resolution_data"]
        # DuplicatePORecipe outputs these fields
        assert "recommended_action" in resolution
        assert "composite_score" in resolution
        assert "classification" in resolution
        assert "autonomy_level" in resolution

    def test_trace_response_shape(self, client, analyst_token):
        """GET /exceptions/{id}/trace — full trace data for the evidence panel."""
        exc_id = self._resolve_duplicate_po(client, analyst_token)
        r = client.get(
            f"/api/v1/exceptions/{exc_id}/trace",
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        # TraceResponse field contract (frontend src/types/exceptions.ts TraceRecord)
        assert isinstance(data["trace_id"], str)
        assert data["event_id"] == "PO-DUP-001"
        assert data["intent_selected"] == "DUPLICATE_PO"
        assert data["shadow_verdict"] == "GREEN"
        assert isinstance(data["shadow_policy_hits"], list)
        assert data["recipe_name"] == "DuplicatePORecipe.py"
        assert isinstance(data["constrained_output_schemas"], dict)
        assert isinstance(data["gateway_calls"], list)
        assert data["final_status"] is not None
        assert isinstance(data["explanation"], str)
        assert "backend_fallback" in data
        assert "is_fallback_generated" in data

    def test_stats_reflect_duplicate_po(self, client, analyst_token):
        """GET /exceptions/stats — dashboard metrics after DUPLICATE_PO resolution."""
        self._resolve_duplicate_po(client, analyst_token)
        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        assert r.status_code == 200
        data = r.json()

        # StatsResponse field contract (frontend src/types/api.ts)
        assert data["total_exceptions"] >= 1
        assert isinstance(data["open_exceptions"], int)
        assert isinstance(data["auto_resolved"], int)
        assert isinstance(data["manual_review"], int)
        assert isinstance(data["blocked"], int)
        assert isinstance(data["failed"], int)
        assert "avg_resolution_time_seconds" in data

        # Breakdown dimensions for Dashboard page
        assert isinstance(data["by_intent"], dict)
        assert isinstance(data["by_lifecycle_state"], dict)
        assert isinstance(data["by_shadow_verdict"], dict)

        # DUPLICATE_PO should appear in by_intent breakdown
        assert "DUPLICATE_PO" in data["by_intent"]
        assert data["by_intent"]["DUPLICATE_PO"] >= 1

    def test_tenant_isolation(self, client, analyst_token, tenant_b_token):
        """Tenant B cannot see Tenant A's DUPLICATE_PO exceptions."""
        self._resolve_duplicate_po(client, analyst_token)

        # Tenant B sees nothing
        r = client.get("/api/v1/exceptions", headers=_auth(tenant_b_token))
        assert r.status_code == 200
        assert len(r.json()["data"]) == 0

        # Tenant B stats show zero
        r = client.get("/api/v1/exceptions/stats", headers=_auth(tenant_b_token))
        assert r.status_code == 200
        assert r.json()["total_exceptions"] == 0


# ---------------------------------------------------------------------------
# 4. HITL actions on DUPLICATE_PO exceptions
# ---------------------------------------------------------------------------

class TestDuplicatePOHitlActions:
    """Approve/Reject/Override flows for DUPLICATE_PO exceptions.

    The frontend AgentReasoningCard component triggers these endpoints.
    """

    def _create_pending_duplicate_po(self, client, token) -> str:
        """Create a DUPLICATE_PO exception in PENDING_REVIEW via explain mode."""
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_duplicate_po_high_confidence(),
            headers=_auth(token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"
        return data["exception_id"]

    def test_approve_duplicate_po(self, client, analyst_token, manager_token):
        """Manager approves a PENDING_REVIEW DUPLICATE_PO exception."""
        exc_id = self._create_pending_duplicate_po(client, analyst_token)

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/approve",
            json={"notes": "Verified — duplicate confirmed by buyer"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["lifecycle_state"] == "EXECUTING"

    def test_reject_duplicate_po(self, client, analyst_token, manager_token):
        """Manager rejects a PENDING_REVIEW DUPLICATE_PO exception."""
        exc_id = self._create_pending_duplicate_po(client, analyst_token)

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reject",
            json={"reason": "Not a duplicate — different delivery date"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["lifecycle_state"] == "REJECTED"

    def test_override_duplicate_po(self, client, analyst_token, manager_token):
        """Manager overrides a PENDING_REVIEW DUPLICATE_PO with a different action."""
        exc_id = self._create_pending_duplicate_po(client, analyst_token)

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={
                "action": "ALLOW_BOTH",
                "notes": "Buyer confirmed both POs are intentional",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["resolved_action"] == "ALLOW_BOTH"
        # Auditor identity derives from JWT sub (create_test_token default)
        assert data["resolved_by"] == "test-user"
        assert data["resolution_notes"] == "Buyer confirmed both POs are intentional"
        assert data["lifecycle_state"] == "RESOLVED"

    def test_detail_after_override_shows_override_fields(self, client, analyst_token, manager_token):
        """After override, GET /exceptions/{id} shows the override fields."""
        exc_id = self._create_pending_duplicate_po(client, analyst_token)
        client.patch(
            f"/api/v1/exceptions/{exc_id}/override",
            json={
                "action": "SUPERSEDE",
                "notes": "Original PO superseded",
            },
            headers=_auth(manager_token),
        )

        # Frontend detail panel reads these override fields
        r = client.get(
            f"/api/v1/exceptions/{exc_id}",
            headers=_auth(analyst_token),
        )
        data = r.json()
        assert data["resolved_action"] == "SUPERSEDE"
        assert data["resolved_by"] == "test-user"
        assert data["resolution_notes"] == "Original PO superseded"


# ---------------------------------------------------------------------------
# 5. Explain mode — DUPLICATE_PO dry-run
# ---------------------------------------------------------------------------

class TestDuplicatePOExplainMode:
    """POST /api/v1/exceptions/resolve/explain — dry-run for DUPLICATE_PO.

    The frontend uses this for "What would happen?" previews.
    """

    def test_explain_returns_full_pipeline_without_execution(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_duplicate_po_high_confidence(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        data = r.json()

        # Explain mode runs classification + shadow but stops before execution
        assert data["intent"] == "DUPLICATE_PO"
        assert data["shadow_verdict"] == "GREEN"
        assert data["selected_recipe"] == "DuplicatePORecipe.py"
        assert data["final_status"] == "MANUAL_REVIEW_REQUIRED"
        assert data["explanation"] is not None


# ---------------------------------------------------------------------------
# 6. Multiple DUPLICATE_PO exceptions — list ordering and stats
# ---------------------------------------------------------------------------

class TestDuplicatePOMultipleExceptions:
    """Verifies the Exception Queue works with multiple DUPLICATE_PO items."""

    def test_multiple_duplicate_pos_in_list(self, client, analyst_token):
        """Resolve multiple DUPLICATE_PO events, verify all appear in list."""
        ids = []
        for event_fn in [_duplicate_po_high_confidence, _duplicate_po_low_confidence]:
            r = client.post(
                "/api/v1/exceptions/resolve",
                json=event_fn(),
                headers=_auth(analyst_token),
            )
            ids.append(r.json()["exception_id"])

        r = client.get("/api/v1/exceptions", headers=_auth(analyst_token))
        data = r.json()["data"]
        assert len(data) == 2
        returned_ids = {exc["id"] for exc in data}
        assert set(ids) == returned_ids

    def test_stats_count_multiple_duplicate_pos(self, client, analyst_token):
        """Stats correctly aggregate multiple DUPLICATE_PO exceptions."""
        for event_fn in [_duplicate_po_high_confidence, _duplicate_po_low_confidence]:
            client.post(
                "/api/v1/exceptions/resolve",
                json=event_fn(),
                headers=_auth(analyst_token),
            )

        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        data = r.json()
        assert data["total_exceptions"] == 2
        assert data["by_intent"]["DUPLICATE_PO"] == 2

    def test_mixed_intents_stats(self, client, analyst_token):
        """Stats correctly distinguish DUPLICATE_PO from pricing exceptions."""
        # One DUPLICATE_PO
        client.post(
            "/api/v1/exceptions/resolve",
            json=_duplicate_po_high_confidence(),
            headers=_auth(analyst_token),
        )
        # One pricing
        client.post(
            "/api/v1/exceptions/resolve",
            json={
                "order_id": "PO-PRICE-001",
                "po_price": 100.0,
                "sap_base_price": 120.0,
                "event_type": "EDI_850_PRICE_MISMATCH",
            },
            headers=_auth(analyst_token),
        )

        r = client.get("/api/v1/exceptions/stats", headers=_auth(analyst_token))
        data = r.json()
        assert data["total_exceptions"] == 2
        assert data["by_intent"].get("DUPLICATE_PO", 0) == 1
        assert data["by_intent"].get("CONTRACTUAL_CORRECTION", 0) == 1
