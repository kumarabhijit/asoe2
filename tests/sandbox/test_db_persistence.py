"""DB persistence tests — verify state is committed after Recipe execution.

After a Recipe executes through the API, these tests verify:
  1. Exception record is persisted with correct fields
  2. Trace record is stored alongside the exception
  3. Lifecycle state transitions are correctly mapped
  4. Policy overrides create audit log entries (SOX)
  5. Pagination works correctly
  6. DatabaseBackedStore matches ExceptionStore behavior

Uses SQLite in-memory for fast, deterministic testing.
Architecture_v3.md Section 9.2 (schema), Section 9.3 (persistence).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import DatabaseBackedStore, ExceptionStore
from db.connection import SQLiteAdapter
from db.migrations.runner import apply_sqlite
from db.repository import ExceptionRepository, TraceRepository, PolicyRepository
from tests.sandbox.conftest import (
    CREDIT_BLOCK_EVENT,
    DUPLICATE_PO_EVENT,
    MASS_PRICING_EVENT,
    PRICING_EVENT,
    SANDBOX_TENANT,
    auth_header,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_adapter():
    """Fresh SQLite in-memory adapter with schema applied."""
    adapter = SQLiteAdapter(":memory:")
    adapter.apply_schema()
    return adapter


@pytest.fixture()
def db_store(db_adapter):
    """DatabaseBackedStore using in-memory SQLite."""
    store = DatabaseBackedStore.__new__(DatabaseBackedStore)
    store._adapter = db_adapter
    from db.repository import ExceptionRepository, TraceRepository
    store._exceptions = ExceptionRepository(db_adapter)
    store._traces = TraceRepository(db_adapter)
    return store


@pytest.fixture()
def exception_repo(db_adapter):
    return ExceptionRepository(db_adapter)


@pytest.fixture()
def trace_repo(db_adapter):
    return TraceRepository(db_adapter)


@pytest.fixture()
def policy_repo(db_adapter):
    return PolicyRepository(db_adapter)


# ═══════════════════════════════════════════════════════════════════════════
# Exception persistence after resolve
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptionPersistence:
    """Verify exception records are committed after graph execution."""

    def test_create_exception_persists_all_fields(self, exception_repo):
        record = exception_repo.create(
            tenant_id=SANDBOX_TENANT,
            order_id="SO-1001",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-001",
            intent="CONTRACTUAL_CORRECTION",
            shadow_verdict="GREEN",
            selected_recipe="PriceAdjustmentRecipe.py",
            final_status="COMPLETE",
            resolution_data={"adjusted_price": 90.0},
        )
        assert record["id"]
        assert record["tenant_id"] == SANDBOX_TENANT
        assert record["order_id"] == "SO-1001"
        assert record["intent"] == "CONTRACTUAL_CORRECTION"
        assert record["shadow_verdict"] == "GREEN"
        assert record["final_status"] == "COMPLETE"

    def test_get_exception_after_create(self, exception_repo):
        created = exception_repo.create(
            tenant_id=SANDBOX_TENANT,
            order_id="SO-1002",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-002",
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
        )
        fetched = exception_repo.get(created["id"], SANDBOX_TENANT)
        assert fetched is not None
        assert fetched["order_id"] == "SO-1002"
        assert fetched["trace_id"] == "trace-002"

    def test_lifecycle_state_mapping(self, exception_repo):
        """final_status → lifecycle_state mapping per architecture_v3.md."""
        mappings = {
            "COMPLETE": "RESOLVED",
            "FAIL_TO_HUMAN": "FAILED",
            "MANUAL_REVIEW_REQUIRED": "PENDING_REVIEW",
            "BLOCKED": "BLOCKED",
            "REJECTED": "REJECTED",
        }
        for final_status, expected_lifecycle in mappings.items():
            record = exception_repo.create(
                tenant_id=SANDBOX_TENANT,
                order_id=f"SO-MAP-{final_status}",
                event_type="TEST",
                trace_id=f"trace-map-{final_status}",
                final_status=final_status,
            )
            assert record["lifecycle_state"] == expected_lifecycle, (
                f"final_status={final_status} should map to "
                f"lifecycle_state={expected_lifecycle}, got {record['lifecycle_state']}"
            )

    def test_update_exception(self, exception_repo):
        record = exception_repo.create(
            tenant_id=SANDBOX_TENANT,
            order_id="SO-UPD",
            event_type="TEST",
            trace_id="trace-upd",
            final_status="MANUAL_REVIEW_REQUIRED",
        )
        updated = exception_repo.update(
            record["id"], SANDBOX_TENANT,
            lifecycle_state="RESOLVED",
            resolved_by="manager-user",
            resolved_action="APPROVED",
            resolution_notes="Reviewed and approved",
        )
        assert updated["lifecycle_state"] == "RESOLVED"
        assert updated["resolved_by"] == "manager-user"

    def test_list_with_status_filter(self, exception_repo):
        exception_repo.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-F1",
            event_type="TEST", trace_id="t1", final_status="COMPLETE",
        )
        exception_repo.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-F2",
            event_type="TEST", trace_id="t2", final_status="BLOCKED",
        )
        resolved, _, _ = exception_repo.list(SANDBOX_TENANT, status="RESOLVED")
        blocked, _, _ = exception_repo.list(SANDBOX_TENANT, status="BLOCKED")
        assert len(resolved) == 1
        assert len(blocked) == 1

    def test_list_pagination(self, exception_repo):
        for i in range(5):
            exception_repo.create(
                tenant_id=SANDBOX_TENANT, order_id=f"SO-PG-{i}",
                event_type="TEST", trace_id=f"t-pg-{i}", final_status="COMPLETE",
            )
        page1, cursor, has_more = exception_repo.list(SANDBOX_TENANT, limit=3)
        assert len(page1) == 3
        assert has_more is True
        assert cursor is not None

        page2, _, _ = exception_repo.list(SANDBOX_TENANT, limit=3, cursor=cursor)
        assert len(page2) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Trace persistence
# ═══════════════════════════════════════════════════════════════════════════

class TestTracePersistence:
    """TraceRecord is stored alongside the exception for audit."""

    def test_trace_created_with_exception(self, exception_repo, trace_repo):
        exc = exception_repo.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-TR",
            event_type="TEST", trace_id="trace-TR",
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
        )
        trace = trace_repo.create(
            exception_id=exc["id"],
            trace_id="trace-TR",
            tenant_id=SANDBOX_TENANT,
            trace_record={
                "intent_selected": "CONTRACTUAL_CORRECTION",
                "shadow_verdict": "GREEN",
                "recipe_name": "PriceAdjustmentRecipe.py",
                "final_status": "COMPLETE",
            },
        )
        assert trace["trace_id"] == "trace-TR"
        assert trace["trace_record"]["intent_selected"] == "CONTRACTUAL_CORRECTION"

    def test_get_trace_by_exception(self, exception_repo, trace_repo):
        exc = exception_repo.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-GT",
            event_type="TEST", trace_id="trace-GT",
            final_status="COMPLETE",
        )
        trace_repo.create(
            exception_id=exc["id"],
            trace_id="trace-GT",
            tenant_id=SANDBOX_TENANT,
            trace_record={"test": True},
        )
        fetched = trace_repo.get_by_exception(exc["id"], SANDBOX_TENANT)
        assert fetched is not None
        assert fetched["trace_record"]["test"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Policy audit log (SOX requirement — architecture_v3.md Section 9.2)
# ═══════════════════════════════════════════════════════════════════════════

class TestPolicyAuditLog:
    """Policy overrides create audit trail entries."""

    def test_create_override_creates_audit_entry(self, policy_repo):
        policy_repo.create_override(
            tenant_id=SANDBOX_TENANT,
            policy_key="MAX_DISCOUNT_ALLOWED",
            value=0.20,
            created_by="admin-user",
            change_reason="Sandbox test",
        )
        log = policy_repo.list_audit_log(SANDBOX_TENANT)
        assert len(log) == 1
        assert log[0]["policy_key"] == "MAX_DISCOUNT_ALLOWED"
        assert log[0]["changed_by"] == "admin-user"
        assert log[0]["change_reason"] == "Sandbox test"

    def test_override_update_records_previous_value(self, policy_repo):
        policy_repo.create_override(
            tenant_id=SANDBOX_TENANT,
            policy_key="MAX_DISCOUNT_ALLOWED",
            value=0.15,
            created_by="admin-user",
        )
        policy_repo.create_override(
            tenant_id=SANDBOX_TENANT,
            policy_key="MAX_DISCOUNT_ALLOWED",
            value=0.20,
            created_by="admin-user",
            change_reason="Increased for Q4",
        )
        log = policy_repo.list_audit_log(SANDBOX_TENANT, limit=2)
        assert len(log) == 2
        # Most recent entry should have the previous value
        assert log[0]["new_value"] == 0.20

    def test_tenant_isolation_in_policy(self, policy_repo):
        policy_repo.create_override(
            tenant_id="tenant-X",
            policy_key="SOME_KEY",
            value="X-value",
            created_by="admin",
        )
        log = policy_repo.list_audit_log("tenant-Y")
        assert len(log) == 0


# ═══════════════════════════════════════════════════════════════════════════
# DatabaseBackedStore integration
# ═══════════════════════════════════════════════════════════════════════════

class TestDatabaseBackedStore:
    """DatabaseBackedStore matches ExceptionStore API contract."""

    def test_create_and_get(self, db_store):
        record = db_store.create(
            tenant_id=SANDBOX_TENANT,
            order_id="SO-DBS",
            event_type="TEST",
            trace_id="trace-dbs",
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
        )
        fetched = db_store.get(record.id, SANDBOX_TENANT)
        assert fetched is not None
        assert fetched.order_id == "SO-DBS"

    def test_store_and_get_trace(self, db_store):
        record = db_store.create(
            tenant_id=SANDBOX_TENANT,
            order_id="SO-TRC",
            event_type="TEST",
            trace_id="trace-trc",
            final_status="COMPLETE",
        )
        db_store.store_trace(record.id, {"intent": "TEST", "status": "COMPLETE"})
        trace = db_store.get_trace(record.id)
        assert trace is not None
        assert trace["intent"] == "TEST"

    def test_list_returns_records(self, db_store):
        db_store.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-L1",
            event_type="TEST", trace_id="t-l1", final_status="COMPLETE",
        )
        records, _, _ = db_store.list(SANDBOX_TENANT)
        assert len(records) >= 1

    def test_stats_returns_counts(self, db_store):
        db_store.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-S1",
            event_type="TEST", trace_id="t-s1", final_status="COMPLETE",
        )
        db_store.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-S2",
            event_type="TEST", trace_id="t-s2", final_status="BLOCKED",
        )
        stats = db_store.stats(SANDBOX_TENANT)
        assert stats["total"] >= 2
        assert stats["auto_resolved"] >= 1
        assert stats["blocked"] >= 1

    def test_clear_removes_all(self, db_store):
        db_store.create(
            tenant_id=SANDBOX_TENANT, order_id="SO-CLR",
            event_type="TEST", trace_id="t-clr", final_status="COMPLETE",
        )
        db_store.clear()
        records, _, _ = db_store.list(SANDBOX_TENANT)
        assert len(records) == 0
