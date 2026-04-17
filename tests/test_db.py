"""Tests for the database layer (db/ module).

Uses SQLite in-memory for all tests — no PostgreSQL required.
Covers: schema migration, exception CRUD, trace storage, policy
overrides with audit log, tenant isolation, lifecycle state mapping,
pagination, and the DatabaseBackedStore API integration.
"""

from __future__ import annotations

import sqlite3

import pytest

from db.connection import SQLiteAdapter, create_adapter
from db.migrations.runner import apply_sqlite
from db.repository import ExceptionRepository, TraceRepository, PolicyRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter():
    """Create a fresh SQLite in-memory adapter with schema applied."""
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


@pytest.fixture()
def exception_repo(adapter):
    return ExceptionRepository(adapter)


@pytest.fixture()
def trace_repo(adapter):
    return TraceRepository(adapter)


@pytest.fixture()
def policy_repo(adapter):
    return PolicyRepository(adapter)


# ---------------------------------------------------------------------------
# Schema / Migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_sqlite_schema_creates_all_tables(self, adapter):
        with adapter.cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [r[0] for r in cur.fetchall()]
        assert "exceptions" in tables
        assert "traces" in tables
        assert "policy_overrides" in tables
        assert "policy_audit_log" in tables
        assert "checkpoints" in tables
        assert "schema_migrations" in tables

    def test_migration_version_recorded(self, adapter):
        with adapter.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations")
            versions = [r[0] for r in cur.fetchall()]
        assert "V001" in versions
        # V002 promotes reanalyze fields to dedicated columns.
        assert "V002" in versions

    def test_v002_columns_exist(self, adapter):
        """V002 adds original_event and reanalysis_history to exceptions."""
        with adapter.cursor() as cur:
            cur.execute("PRAGMA table_info(exceptions)")
            cols = {row[1] for row in cur.fetchall()}
        assert "original_event" in cols
        assert "reanalysis_history" in cols

    def test_idempotent_migration(self, adapter):
        """Applying schema twice should not raise."""
        adapter.apply_schema()
        with adapter.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            count = cur.fetchone()[0]
        # Should have 1 or 2 rows (INSERT OR IGNORE), no error
        assert count >= 1

    def test_create_adapter_sqlite(self):
        a = create_adapter("sqlite://:memory:")
        assert isinstance(a, SQLiteAdapter)

    def test_create_adapter_default(self):
        """No DATABASE_URL → SQLiteAdapter."""
        import os
        prev = os.environ.pop("DATABASE_URL", None)
        try:
            a = create_adapter()
            assert isinstance(a, SQLiteAdapter)
        finally:
            if prev:
                os.environ["DATABASE_URL"] = prev


# ---------------------------------------------------------------------------
# Exception Repository
# ---------------------------------------------------------------------------

class TestExceptionRepository:
    def test_create_and_get(self, exception_repo):
        record = exception_repo.create(
            tenant_id="t1",
            order_id="PO-001",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-001",
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
        )
        assert record["id"]
        assert record["tenant_id"] == "t1"
        assert record["lifecycle_state"] == "RESOLVED"  # maps from COMPLETE

        fetched = exception_repo.get(record["id"], "t1")
        assert fetched is not None
        assert fetched["order_id"] == "PO-001"
        assert fetched["intent"] == "CONTRACTUAL_CORRECTION"

    def test_get_wrong_tenant_returns_none(self, exception_repo):
        record = exception_repo.create(
            tenant_id="t1",
            order_id="PO-002",
            event_type="CREDIT_BLOCK",
            trace_id="trace-002",
        )
        assert exception_repo.get(record["id"], "t2") is None

    def test_lifecycle_state_mapping(self, exception_repo):
        """final_status maps to lifecycle_state per architecture_v3.md §9.1."""
        mappings = {
            "COMPLETE": "RESOLVED",
            "FAIL_TO_HUMAN": "FAILED",
            "MANUAL_REVIEW_REQUIRED": "PENDING_REVIEW",
            "BLOCKED": "BLOCKED",
            "REJECTED": "REJECTED",
        }
        for status, expected_lifecycle in mappings.items():
            r = exception_repo.create(
                tenant_id="t1",
                order_id=f"PO-{status}",
                event_type="TEST",
                trace_id=f"trace-{status}",
                final_status=status,
            )
            assert r["lifecycle_state"] == expected_lifecycle

    def test_list_basic(self, exception_repo):
        exception_repo.create(
            tenant_id="t1", order_id="PO-A", event_type="TEST",
            trace_id="t-a", final_status="COMPLETE",
        )
        exception_repo.create(
            tenant_id="t1", order_id="PO-B", event_type="TEST",
            trace_id="t-b", final_status="BLOCKED",
        )
        records, cursor, has_more = exception_repo.list("t1")
        assert len(records) == 2
        assert not has_more

    def test_list_filter_by_status(self, exception_repo):
        exception_repo.create(
            tenant_id="t1", order_id="PO-X", event_type="TEST",
            trace_id="t-x", final_status="COMPLETE",
        )
        exception_repo.create(
            tenant_id="t1", order_id="PO-Y", event_type="TEST",
            trace_id="t-y", final_status="BLOCKED",
        )
        records, _, _ = exception_repo.list("t1", status="BLOCKED")
        assert len(records) == 1
        assert records[0]["order_id"] == "PO-Y"

    def test_list_filter_by_intent(self, exception_repo):
        exception_repo.create(
            tenant_id="t1", order_id="PO-1", event_type="TEST",
            trace_id="t-1", intent="CREDIT_BLOCK",
        )
        exception_repo.create(
            tenant_id="t1", order_id="PO-2", event_type="TEST",
            trace_id="t-2", intent="DUPLICATE_PO",
        )
        records, _, _ = exception_repo.list("t1", intent="CREDIT_BLOCK")
        assert len(records) == 1

    def test_list_tenant_isolation(self, exception_repo):
        exception_repo.create(
            tenant_id="t1", order_id="PO-T1", event_type="TEST", trace_id="tt1",
        )
        exception_repo.create(
            tenant_id="t2", order_id="PO-T2", event_type="TEST", trace_id="tt2",
        )
        records_t1, _, _ = exception_repo.list("t1")
        records_t2, _, _ = exception_repo.list("t2")
        assert len(records_t1) == 1
        assert len(records_t2) == 1
        assert records_t1[0]["order_id"] == "PO-T1"

    def test_list_pagination(self, exception_repo):
        for i in range(5):
            exception_repo.create(
                tenant_id="t1", order_id=f"PO-{i}",
                event_type="TEST", trace_id=f"t-{i}",
            )
        page1, cursor1, has_more1 = exception_repo.list("t1", limit=2)
        assert len(page1) == 2
        assert has_more1
        assert cursor1 is not None

        page2, cursor2, has_more2 = exception_repo.list("t1", limit=2, cursor=cursor1)
        assert len(page2) == 2
        # IDs should not overlap
        page1_ids = {r["id"] for r in page1}
        page2_ids = {r["id"] for r in page2}
        assert page1_ids.isdisjoint(page2_ids)

    def test_update(self, exception_repo):
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-UPD", event_type="TEST",
            trace_id="t-upd", final_status="MANUAL_REVIEW_REQUIRED",
        )
        updated = exception_repo.update(
            record["id"], "t1",
            resolved_by="human@example.com",
            resolved_action="ALLOW_BOTH",
            lifecycle_state="RESOLVED",
        )
        assert updated is not None
        assert updated["resolved_by"] == "human@example.com"
        assert updated["lifecycle_state"] == "RESOLVED"

    def test_update_wrong_tenant(self, exception_repo):
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-WR", event_type="TEST",
            trace_id="t-wr",
        )
        result = exception_repo.update(record["id"], "t2", resolved_by="hacker")
        assert result is None

    def test_stats(self, exception_repo):
        exception_repo.create(
            tenant_id="t1", order_id="PO-1", event_type="T",
            trace_id="s1", final_status="COMPLETE",
        )
        exception_repo.create(
            tenant_id="t1", order_id="PO-2", event_type="T",
            trace_id="s2", final_status="BLOCKED",
        )
        exception_repo.create(
            tenant_id="t1", order_id="PO-3", event_type="T",
            trace_id="s3", final_status="MANUAL_REVIEW_REQUIRED",
        )
        stats = exception_repo.stats("t1")
        assert stats["total_exceptions"] == 3
        assert stats["auto_resolved"] == 1
        assert stats["blocked"] == 1
        assert stats["manual_review"] == 1

    def test_resolution_data_json(self, exception_repo):
        res_data = {"recommended_action": "BLOCK_AND_NOTIFY", "score": 0.95}
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-JSON", event_type="TEST",
            trace_id="t-json", resolution_data=res_data,
        )
        fetched = exception_repo.get(record["id"], "t1")
        assert fetched["resolution_data"] == res_data

    def test_intent_check_constraint(self, exception_repo):
        """Only valid intents are accepted per architecture_v3.md §9.2."""
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-CHK", event_type="TEST",
            trace_id="t-chk", intent="CONTRACTUAL_CORRECTION",
        )
        assert record["intent"] == "CONTRACTUAL_CORRECTION"

        with pytest.raises(Exception):
            exception_repo.create(
                tenant_id="t1", order_id="PO-BAD", event_type="TEST",
                trace_id="t-bad", intent="INVALID_INTENT",
            )

    def test_original_event_roundtrip(self, exception_repo):
        """V002: original_event persists as JSON and deserialises back."""
        event = {
            "order_id": "PO-555", "event_type": "EDI_850",
            "po_price": 100.0, "sap_base_price": 120.0,
        }
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-555", event_type="EDI_850",
            trace_id="t-555", original_event=event,
        )
        fetched = exception_repo.get(record["id"], "t1")
        assert fetched["original_event"] == event

    def test_reanalysis_history_defaults_empty(self, exception_repo):
        """V002: reanalysis_history defaults to [] for new records."""
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-556", event_type="TEST",
            trace_id="t-556",
        )
        fetched = exception_repo.get(record["id"], "t1")
        assert fetched["reanalysis_history"] == []

    def test_reanalysis_history_update(self, exception_repo):
        """V002: reanalysis_history can be updated with structured entries."""
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-557", event_type="TEST",
            trace_id="t-557",
        )
        entry = {
            "attempt": 1, "triggered_by": "manager@tenant-a",
            "reason": "Buyer sent update", "new_trace_id": "t-557-new",
        }
        exception_repo.update(record["id"], "t1", reanalysis_history=[entry])
        fetched = exception_repo.get(record["id"], "t1")
        assert len(fetched["reanalysis_history"]) == 1
        assert fetched["reanalysis_history"][0]["attempt"] == 1
        assert fetched["reanalysis_history"][0]["reason"] == "Buyer sent update"


# ---------------------------------------------------------------------------
# Trace Repository
# ---------------------------------------------------------------------------

class TestTraceRepository:
    def test_create_and_get(self, adapter, trace_repo):
        # First create an exception to reference
        exc_repo = ExceptionRepository(adapter)
        exc = exc_repo.create(
            tenant_id="t1", order_id="PO-TR", event_type="TEST",
            trace_id="tr-001",
        )

        trace_data = {
            "trace_id": "tr-001",
            "event_id": "PO-TR",
            "intent_selected": "CONTRACTUAL_CORRECTION",
            "shadow_verdict": "GREEN",
            "final_status": "COMPLETE",
        }
        trace = trace_repo.create(
            exception_id=exc["id"],
            trace_id="tr-001",
            tenant_id="t1",
            trace_record=trace_data,
        )
        assert trace["id"]

        fetched = trace_repo.get_by_exception(exc["id"], "t1")
        assert fetched is not None
        assert fetched["trace_record"]["intent_selected"] == "CONTRACTUAL_CORRECTION"

    def test_trace_tenant_isolation(self, adapter, trace_repo):
        exc_repo = ExceptionRepository(adapter)
        exc = exc_repo.create(
            tenant_id="t1", order_id="PO-ISO", event_type="TEST",
            trace_id="iso-001",
        )
        trace_repo.create(
            exception_id=exc["id"], trace_id="iso-001",
            tenant_id="t1", trace_record={"status": "ok"},
        )
        assert trace_repo.get_by_exception(exc["id"], "t2") is None


# ---------------------------------------------------------------------------
# Policy Repository
# ---------------------------------------------------------------------------

class TestPolicyRepository:
    def test_create_override_with_audit(self, policy_repo):
        result = policy_repo.create_override(
            tenant_id="t1",
            policy_key="global.MAX_DISCOUNT_ALLOWED",
            value=0.25,
            created_by="admin@example.com",
            change_reason="Seasonal adjustment",
        )
        assert result["policy_key"] == "global.MAX_DISCOUNT_ALLOWED"
        assert result["value"] == 0.25

        # Verify audit log entry was created
        audit = policy_repo.list_audit_log("t1")
        assert len(audit) == 1
        assert audit[0]["policy_key"] == "global.MAX_DISCOUNT_ALLOWED"
        assert audit[0]["new_value"] == 0.25
        assert audit[0]["changed_by"] == "admin@example.com"
        assert audit[0]["change_reason"] == "Seasonal adjustment"
        assert audit[0]["previous_value"] is None  # no prior value

    def test_update_override_records_previous(self, policy_repo):
        """Second override records the previous value in audit log."""
        policy_repo.create_override(
            tenant_id="t1", policy_key="global.THRESHOLD",
            value=0.15, created_by="admin1",
        )
        policy_repo.create_override(
            tenant_id="t1", policy_key="global.THRESHOLD",
            value=0.20, created_by="admin2",
        )
        audit = policy_repo.list_audit_log("t1")
        assert len(audit) == 2
        # Most recent first
        assert audit[0]["new_value"] == 0.20
        assert audit[0]["previous_value"] == 0.15

    def test_get_latest_override(self, policy_repo):
        policy_repo.create_override(
            tenant_id="t1", policy_key="global.X", value=1, created_by="a",
        )
        policy_repo.create_override(
            tenant_id="t1", policy_key="global.X", value=2, created_by="b",
        )
        latest = policy_repo.get_override("t1", "global.X")
        assert latest is not None
        assert latest["value"] == 2

    def test_policy_tenant_isolation(self, policy_repo):
        policy_repo.create_override(
            tenant_id="t1", policy_key="global.Y", value=99, created_by="a",
        )
        assert policy_repo.get_override("t2", "global.Y") is None


# ---------------------------------------------------------------------------
# DatabaseBackedStore (API integration)
# ---------------------------------------------------------------------------

class TestDatabaseBackedStore:
    def test_store_round_trip(self):
        """Verify DatabaseBackedStore works as a drop-in for ExceptionStore."""
        from api.store import DatabaseBackedStore
        store = DatabaseBackedStore("sqlite://:memory:")

        record = store.create(
            tenant_id="t1",
            order_id="PO-STORE",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="store-trace-001",
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
        )
        assert record.id
        assert record.lifecycle_state == "RESOLVED"

        fetched = store.get(record.id, "t1")
        assert fetched is not None
        assert fetched.order_id == "PO-STORE"

    def test_store_list(self):
        from api.store import DatabaseBackedStore
        store = DatabaseBackedStore("sqlite://:memory:")

        store.create(
            tenant_id="t1", order_id="A", event_type="T",
            trace_id="a", final_status="COMPLETE",
        )
        store.create(
            tenant_id="t1", order_id="B", event_type="T",
            trace_id="b", final_status="BLOCKED",
        )
        records, _, _ = store.list("t1")
        assert len(records) == 2

    def test_store_trace(self):
        from api.store import DatabaseBackedStore
        store = DatabaseBackedStore("sqlite://:memory:")

        record = store.create(
            tenant_id="t1", order_id="TR", event_type="T", trace_id="tr",
        )
        trace_data = {"trace_id": "tr", "event_id": "TR", "intent": "UNKNOWN"}
        store.store_trace(record.id, trace_data)

        fetched_trace = store.get_trace(record.id)
        assert fetched_trace is not None
        assert fetched_trace["event_id"] == "TR"

    def test_store_update(self):
        from api.store import DatabaseBackedStore
        store = DatabaseBackedStore("sqlite://:memory:")

        record = store.create(
            tenant_id="t1", order_id="UPD", event_type="T",
            trace_id="upd", final_status="MANUAL_REVIEW_REQUIRED",
        )
        updated = store.update(
            record.id, "t1",
            lifecycle_state="RESOLVED",
            resolved_by="human",
        )
        assert updated is not None
        assert updated.lifecycle_state == "RESOLVED"
        assert updated.resolved_by == "human"

    def test_store_stats(self):
        from api.store import DatabaseBackedStore
        store = DatabaseBackedStore("sqlite://:memory:")

        store.create(
            tenant_id="t1", order_id="S1", event_type="T",
            trace_id="s1", final_status="COMPLETE",
        )
        store.create(
            tenant_id="t1", order_id="S2", event_type="T",
            trace_id="s2", final_status="BLOCKED",
        )
        stats = store.stats("t1")
        assert stats["total_exceptions"] == 2
        assert stats["auto_resolved"] == 1
        assert stats["blocked"] == 1

    def test_store_clear(self):
        from api.store import DatabaseBackedStore
        store = DatabaseBackedStore("sqlite://:memory:")

        store.create(
            tenant_id="t1", order_id="CLR", event_type="T", trace_id="c",
        )
        store.clear()
        records, _, _ = store.list("t1")
        assert len(records) == 0

    def test_store_tenant_isolation(self):
        from api.store import DatabaseBackedStore
        store = DatabaseBackedStore("sqlite://:memory:")

        record = store.create(
            tenant_id="t1", order_id="ISO", event_type="T", trace_id="iso",
        )
        assert store.get(record.id, "t2") is None
