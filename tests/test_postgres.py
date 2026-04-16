"""PostgreSQL integration tests.

Exercises the PostgreSQL-specific features that SQLite tests cannot cover:

  1. PostgresAdapter — connection, cursor wrapper (?→%s translation)
  2. Schema migration — V001 applied via apply_postgres()
  3. PostgreSQL types — UUID primary keys, JSONB columns, TIMESTAMPTZ
  4. Row-Level Security — tenant isolation enforced at database level
  5. SOX immutability trigger — policy_audit_log rejects UPDATE/DELETE
  6. Repository CRUD — same operations as test_db.py but on real PostgreSQL
  7. DatabaseBackedStore — full API store backed by PostgreSQL

Requires a running PostgreSQL instance. Tests are skipped when
the ASOE_TEST_POSTGRES_URL environment variable is not set or
the connection fails.

Usage:
    ASOE_TEST_POSTGRES_URL=postgresql://asoe_test:asoe_test@localhost/asoe_test \
        pytest tests/test_postgres.py -v
"""

from __future__ import annotations

import os
import json
import logging
from uuid import uuid4

import pytest

logger = logging.getLogger("asoe.tests.postgres")


def _uid() -> str:
    """Generate a UUID string for PostgreSQL UUID columns."""
    return str(uuid4())

# ---------------------------------------------------------------------------
# Skip if no PostgreSQL available
# ---------------------------------------------------------------------------

_PG_URL = os.getenv(
    "ASOE_TEST_POSTGRES_URL",
    "postgresql://asoe_test:asoe_test@localhost:5432/asoe_test",
)


def _pg_available() -> bool:
    """Check if PostgreSQL is reachable."""
    try:
        import psycopg2
        conn = psycopg2.connect(_PG_URL)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL not available (set ASOE_TEST_POSTGRES_URL)",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def pg_adapter():
    """Create a PostgresAdapter, apply schema, and clean up after test."""
    from db.connection import PostgresAdapter

    adapter = PostgresAdapter(_PG_URL)
    adapter.apply_schema()

    yield adapter

    # Teardown: truncate all tables (preserve schema for next test)
    with adapter.connection() as conn:
        cur = conn.cursor()
        cur.execute("SET app.current_tenant_id = 'cleanup'")
        # Disable triggers temporarily for cleanup
        cur.execute("ALTER TABLE policy_audit_log DISABLE TRIGGER trg_policy_audit_immutable")
        cur.execute("TRUNCATE traces, policy_audit_log, policy_overrides, checkpoints, exceptions CASCADE")
        cur.execute("ALTER TABLE policy_audit_log ENABLE TRIGGER trg_policy_audit_immutable")
        conn.commit()
        cur.close()
    adapter.close()


@pytest.fixture()
def exception_repo(pg_adapter):
    from db.repository import ExceptionRepository
    return ExceptionRepository(pg_adapter)


@pytest.fixture()
def trace_repo(pg_adapter):
    from db.repository import TraceRepository
    return TraceRepository(pg_adapter)


@pytest.fixture()
def policy_repo(pg_adapter):
    from db.repository import PolicyRepository
    return PolicyRepository(pg_adapter)


# ---------------------------------------------------------------------------
# 1. PostgresAdapter — connection and cursor wrapper
# ---------------------------------------------------------------------------

class TestPostgresAdapter:
    """Tests for PostgresAdapter basic operations."""

    def test_connection_established(self, pg_adapter):
        """Adapter can connect to PostgreSQL."""
        with pg_adapter.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
            cur.close()

    def test_cursor_qmark_translation(self, pg_adapter):
        """Cursor wrapper translates ? → %s for PostgreSQL."""
        with pg_adapter.cursor() as cur:
            cur.execute("SELECT ? + ?", (1, 2))
            row = cur.fetchone()
            assert row[0] == 3

    def test_tenant_id_session_variable(self, pg_adapter):
        """connection(tenant_id) sets app.current_tenant_id session var."""
        with pg_adapter.connection("test-tenant") as conn:
            cur = conn.cursor()
            cur.execute("SELECT current_setting('app.current_tenant_id', true)")
            val = cur.fetchone()[0]
            assert val == "test-tenant"
            cur.close()


# ---------------------------------------------------------------------------
# 2. Schema migration
# ---------------------------------------------------------------------------

class TestPostgresMigration:
    """Tests for PostgreSQL schema migration (V001)."""

    def test_all_tables_created(self, pg_adapter):
        """V001 migration creates all required tables."""
        with pg_adapter.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)
            tables = [r[0] for r in cur.fetchall()]
        assert "exceptions" in tables
        assert "traces" in tables
        assert "policy_overrides" in tables
        assert "policy_audit_log" in tables
        assert "checkpoints" in tables
        assert "schema_migrations" in tables

    def test_migration_version_recorded(self, pg_adapter):
        """V001 is recorded in schema_migrations."""
        with pg_adapter.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations")
            versions = [r[0] for r in cur.fetchall()]
        assert "V001" in versions

    def test_idempotent_migration(self, pg_adapter):
        """Applying schema twice does not raise."""
        pg_adapter.apply_schema()  # Second apply
        with pg_adapter.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            count = cur.fetchone()[0]
        assert count >= 1

    def test_pgcrypto_extension_installed(self, pg_adapter):
        """pgcrypto extension is available (gen_random_uuid)."""
        with pg_adapter.cursor() as cur:
            cur.execute("SELECT gen_random_uuid()")
            uuid_val = cur.fetchone()[0]
        assert len(str(uuid_val)) == 36  # UUID format

    def test_uuid_primary_keys(self, pg_adapter):
        """exceptions.id is UUID type, not TEXT."""
        with pg_adapter.cursor() as cur:
            cur.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'exceptions' AND column_name = 'id'
            """)
            dtype = cur.fetchone()[0]
        assert dtype == "uuid"

    def test_jsonb_columns(self, pg_adapter):
        """resolution_data and trace_record use JSONB, not TEXT."""
        with pg_adapter.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name = 'exceptions' AND column_name = 'resolution_data'
            """)
            row = cur.fetchone()
        assert row[1] == "jsonb"

    def test_timestamptz_columns(self, pg_adapter):
        """created_at and updated_at use TIMESTAMPTZ."""
        with pg_adapter.cursor() as cur:
            cur.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'exceptions' AND column_name = 'created_at'
            """)
            dtype = cur.fetchone()[0]
        assert "timestamp" in dtype


# ---------------------------------------------------------------------------
# 3. Row-Level Security (RLS)
# ---------------------------------------------------------------------------

class TestRowLevelSecurity:
    """Tests for PostgreSQL RLS enforcement (architecture_v3.md §11.3)."""

    def test_rls_enabled_on_exceptions(self, pg_adapter):
        """RLS is enabled on the exceptions table."""
        with pg_adapter.cursor() as cur:
            cur.execute("""
                SELECT rowsecurity FROM pg_tables
                WHERE tablename = 'exceptions'
            """)
            rls_enabled = cur.fetchone()[0]
        assert rls_enabled is True

    def test_rls_enabled_on_traces(self, pg_adapter):
        """RLS is enabled on the traces table."""
        with pg_adapter.cursor() as cur:
            cur.execute("""
                SELECT rowsecurity FROM pg_tables
                WHERE tablename = 'traces'
            """)
            rls_enabled = cur.fetchone()[0]
        assert rls_enabled is True

    def test_rls_tenant_isolation_via_repository(self, exception_repo):
        """Tenant A cannot see Tenant B's exceptions through the repository."""
        # Create exception for tenant-a
        exception_repo.create(
            tenant_id="tenant-a", order_id="PO-RLS-A",
            event_type="TEST", trace_id=_uid(),
            final_status="COMPLETE",
        )
        # Create exception for tenant-b
        exception_repo.create(
            tenant_id="tenant-b", order_id="PO-RLS-B",
            event_type="TEST", trace_id=_uid(),
            final_status="COMPLETE",
        )

        # Tenant-a sees only their own
        records_a, _, _ = exception_repo.list("tenant-a")
        assert len(records_a) == 1
        assert records_a[0]["order_id"] == "PO-RLS-A"

        # Tenant-b sees only their own
        records_b, _, _ = exception_repo.list("tenant-b")
        assert len(records_b) == 1
        assert records_b[0]["order_id"] == "PO-RLS-B"

    def test_rls_get_wrong_tenant_returns_none(self, exception_repo):
        """GET by ID with wrong tenant returns None."""
        record = exception_repo.create(
            tenant_id="tenant-a", order_id="PO-RLS-X",
            event_type="TEST", trace_id=_uid(),
        )
        assert exception_repo.get(record["id"], "tenant-b") is None


# ---------------------------------------------------------------------------
# 4. SOX immutability trigger
# ---------------------------------------------------------------------------

class TestSOXAuditImmutability:
    """Tests for the SOX compliance trigger on policy_audit_log."""

    def test_audit_log_insert_succeeds(self, policy_repo):
        """Normal insert into policy_audit_log works."""
        result = policy_repo.create_override(
            tenant_id="t1", policy_key="test.key",
            value=42, created_by="admin",
            change_reason="Initial value",
        )
        assert result["value"] == 42

        audit = policy_repo.list_audit_log("t1")
        assert len(audit) == 1

    def test_audit_log_update_blocked(self, pg_adapter, policy_repo):
        """UPDATE on policy_audit_log is blocked by SOX trigger."""
        policy_repo.create_override(
            tenant_id="t1", policy_key="sox.key",
            value=1, created_by="admin",
        )

        with pytest.raises(Exception, match="immutable"):
            with pg_adapter.connection("t1") as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE policy_audit_log SET new_value = '999' "
                    "WHERE tenant_id = %s", ("t1",)
                )
                conn.commit()
                cur.close()

    def test_audit_log_delete_blocked(self, pg_adapter, policy_repo):
        """DELETE on policy_audit_log is blocked by SOX trigger."""
        policy_repo.create_override(
            tenant_id="t1", policy_key="sox.del",
            value=1, created_by="admin",
        )

        with pytest.raises(Exception, match="immutable"):
            with pg_adapter.connection("t1") as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM policy_audit_log WHERE tenant_id = %s", ("t1",)
                )
                conn.commit()
                cur.close()


# ---------------------------------------------------------------------------
# 5. Repository CRUD on PostgreSQL
# ---------------------------------------------------------------------------

class TestExceptionRepositoryPostgres:
    """Exception CRUD on real PostgreSQL (mirrors test_db.py)."""

    def test_create_and_get(self, exception_repo):
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-PG-001",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id=_uid(),
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
        )
        assert record["id"]
        assert record["lifecycle_state"] == "RESOLVED"

        fetched = exception_repo.get(record["id"], "t1")
        assert fetched is not None
        assert fetched["order_id"] == "PO-PG-001"
        assert fetched["intent"] == "CONTRACTUAL_CORRECTION"

    def test_lifecycle_state_mapping(self, exception_repo):
        """final_status maps to lifecycle_state per architecture_v3.md §9.1."""
        mappings = {
            "COMPLETE": "RESOLVED",
            "FAIL_TO_HUMAN": "FAILED",
            "MANUAL_REVIEW_REQUIRED": "PENDING_REVIEW",
            "BLOCKED": "BLOCKED",
            "REJECTED": "REJECTED",
        }
        for status, expected in mappings.items():
            r = exception_repo.create(
                tenant_id="t1", order_id=f"PO-{status}",
                event_type="TEST", trace_id=_uid(),
                final_status=status,
            )
            assert r["lifecycle_state"] == expected

    def test_list_with_filters(self, exception_repo):
        exception_repo.create(
            tenant_id="t1", order_id="PO-A", event_type="T",
            trace_id=_uid(), intent="CONTRACTUAL_CORRECTION", final_status="COMPLETE",
        )
        exception_repo.create(
            tenant_id="t1", order_id="PO-B", event_type="T",
            trace_id=_uid(), intent="DUPLICATE_PO", final_status="BLOCKED",
        )
        # Filter by intent
        records, _, _ = exception_repo.list("t1", intent="DUPLICATE_PO")
        assert len(records) == 1
        assert records[0]["intent"] == "DUPLICATE_PO"

        # Filter by status
        records, _, _ = exception_repo.list("t1", status="BLOCKED")
        assert len(records) == 1

    def test_pagination(self, exception_repo):
        for i in range(5):
            exception_repo.create(
                tenant_id="t1", order_id=f"PO-PAGE-{i}",
                event_type="T", trace_id=_uid(),
            )
        page1, cursor1, has_more1 = exception_repo.list("t1", limit=2)
        assert len(page1) == 2
        assert has_more1
        assert cursor1 is not None

        page2, _, _ = exception_repo.list("t1", limit=2, cursor=cursor1)
        assert len(page2) == 2
        page1_ids = {r["id"] for r in page1}
        page2_ids = {r["id"] for r in page2}
        assert page1_ids.isdisjoint(page2_ids)

    def test_update(self, exception_repo):
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-UPD",
            event_type="T", trace_id=_uid(),
            final_status="MANUAL_REVIEW_REQUIRED",
        )
        updated = exception_repo.update(
            record["id"], "t1",
            resolved_by="human@example.com",
            lifecycle_state="RESOLVED",
        )
        assert updated is not None
        assert updated["resolved_by"] == "human@example.com"
        assert updated["lifecycle_state"] == "RESOLVED"

    def test_jsonb_resolution_data(self, exception_repo):
        """JSONB round-trips correctly through PostgreSQL."""
        res_data = {
            "recommended_action": "BLOCK_AND_NOTIFY",
            "composite_score": 0.95,
            "signals": {"po_number": 1.0, "customer_id": 0.9},
        }
        record = exception_repo.create(
            tenant_id="t1", order_id="PO-JSONB",
            event_type="T", trace_id=_uid(),
            resolution_data=res_data,
        )
        fetched = exception_repo.get(record["id"], "t1")
        assert fetched["resolution_data"]["recommended_action"] == "BLOCK_AND_NOTIFY"
        assert fetched["resolution_data"]["composite_score"] == 0.95
        assert fetched["resolution_data"]["signals"]["po_number"] == 1.0

    def test_stats(self, exception_repo):
        exception_repo.create(
            tenant_id="t1", order_id="S1", event_type="T",
            trace_id=_uid(), intent="CONTRACTUAL_CORRECTION", final_status="COMPLETE",
        )
        exception_repo.create(
            tenant_id="t1", order_id="S2", event_type="T",
            trace_id=_uid(), intent="DUPLICATE_PO", final_status="BLOCKED",
        )
        exception_repo.create(
            tenant_id="t1", order_id="S3", event_type="T",
            trace_id=_uid(), intent="CREDIT_BLOCK", final_status="MANUAL_REVIEW_REQUIRED",
        )
        stats = exception_repo.stats("t1")
        assert stats["total_exceptions"] == 3
        assert stats["auto_resolved"] == 1
        assert stats["blocked"] == 1
        assert stats["manual_review"] == 1
        assert "CONTRACTUAL_CORRECTION" in stats["by_intent"]
        assert "DUPLICATE_PO" in stats["by_intent"]

    def test_intent_check_constraint(self, exception_repo):
        """Only valid intents are accepted."""
        exception_repo.create(
            tenant_id="t1", order_id="PO-CHK",
            event_type="T", trace_id=_uid(),
            intent="CONTRACTUAL_CORRECTION",
        )
        with pytest.raises(Exception):
            exception_repo.create(
                tenant_id="t1", order_id="PO-BAD",
                event_type="T", trace_id=_uid(),
                intent="INVALID_INTENT",
            )


class TestTraceRepositoryPostgres:
    """Trace CRUD on real PostgreSQL."""

    def test_create_and_get(self, pg_adapter, trace_repo):
        from db.repository import ExceptionRepository
        exc_repo = ExceptionRepository(pg_adapter)
        tid = _uid()
        exc = exc_repo.create(
            tenant_id="t1", order_id="PO-TR",
            event_type="T", trace_id=tid,
        )

        trace_data = {
            "trace_id": tid,
            "event_id": "PO-TR",
            "intent_selected": "CONTRACTUAL_CORRECTION",
            "shadow_verdict": "GREEN",
            "final_status": "COMPLETE",
        }
        trace_repo.create(
            exception_id=exc["id"], trace_id=tid,
            tenant_id="t1", trace_record=trace_data,
        )

        fetched = trace_repo.get_by_exception(exc["id"], "t1")
        assert fetched is not None
        assert fetched["trace_record"]["intent_selected"] == "CONTRACTUAL_CORRECTION"

    def test_trace_tenant_isolation(self, pg_adapter, trace_repo):
        from db.repository import ExceptionRepository
        exc_repo = ExceptionRepository(pg_adapter)
        tid = _uid()
        exc = exc_repo.create(
            tenant_id="t1", order_id="PO-ISO",
            event_type="T", trace_id=tid,
        )
        trace_repo.create(
            exception_id=exc["id"], trace_id=tid,
            tenant_id="t1", trace_record={"status": "ok"},
        )
        assert trace_repo.get_by_exception(exc["id"], "t2") is None


class TestPolicyRepositoryPostgres:
    """Policy override + audit log on real PostgreSQL."""

    def test_create_override_with_audit(self, policy_repo):
        result = policy_repo.create_override(
            tenant_id="t1", policy_key="global.MAX_DISCOUNT",
            value=0.25, created_by="admin@example.com",
            change_reason="Seasonal adjustment",
        )
        assert result["value"] == 0.25

        audit = policy_repo.list_audit_log("t1")
        assert len(audit) == 1
        assert audit[0]["changed_by"] == "admin@example.com"

    def test_update_records_previous_value(self, policy_repo):
        policy_repo.create_override(
            tenant_id="t1", policy_key="global.TH",
            value=0.15, created_by="admin1",
        )
        policy_repo.create_override(
            tenant_id="t1", policy_key="global.TH",
            value=0.20, created_by="admin2",
        )
        audit = policy_repo.list_audit_log("t1")
        assert len(audit) == 2
        assert audit[0]["new_value"] == 0.20
        assert audit[0]["previous_value"] == 0.15


# ---------------------------------------------------------------------------
# 6. DatabaseBackedStore on PostgreSQL
# ---------------------------------------------------------------------------

class TestDatabaseBackedStorePostgres:
    """Full store integration backed by real PostgreSQL."""

    @pytest.fixture()
    def store(self):
        from api.store import DatabaseBackedStore
        s = DatabaseBackedStore(_PG_URL)
        yield s
        s.clear()

    def test_store_round_trip(self, store):
        record = store.create(
            tenant_id="t1", order_id="PO-STORE-PG",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id=_uid(),
            intent="CONTRACTUAL_CORRECTION",
            final_status="COMPLETE",
        )
        assert record.id
        assert record.lifecycle_state == "RESOLVED"

        fetched = store.get(record.id, "t1")
        assert fetched is not None
        assert fetched.order_id == "PO-STORE-PG"

    def test_store_list_and_filter(self, store):
        store.create(
            tenant_id="t1", order_id="A", event_type="T",
            trace_id=_uid(), intent="CONTRACTUAL_CORRECTION", final_status="COMPLETE",
        )
        store.create(
            tenant_id="t1", order_id="B", event_type="T",
            trace_id=_uid(), intent="DUPLICATE_PO", final_status="BLOCKED",
        )
        records, _, _ = store.list("t1")
        assert len(records) == 2

        # Filter by intent
        filtered, _, _ = store.list("t1", intent="DUPLICATE_PO")
        assert len(filtered) == 1

    def test_store_trace(self, store):
        record = store.create(
            tenant_id="t1", order_id="TR", event_type="T", trace_id=_uid(),
        )
        trace_data = {"trace_id": "tr-pg", "event_id": "TR", "intent": "UNKNOWN"}
        store.store_trace(record.id, trace_data)

        fetched = store.get_trace(record.id)
        assert fetched is not None
        assert fetched["event_id"] == "TR"

    def test_store_update(self, store):
        record = store.create(
            tenant_id="t1", order_id="UPD", event_type="T",
            trace_id=_uid(), final_status="MANUAL_REVIEW_REQUIRED",
        )
        updated = store.update(
            record.id, "t1",
            lifecycle_state="RESOLVED",
            resolved_by="human",
        )
        assert updated is not None
        assert updated.lifecycle_state == "RESOLVED"

    def test_store_stats(self, store):
        store.create(
            tenant_id="t1", order_id="S1", event_type="T",
            trace_id=_uid(), final_status="COMPLETE",
        )
        store.create(
            tenant_id="t1", order_id="S2", event_type="T",
            trace_id=_uid(), final_status="BLOCKED",
        )
        stats = store.stats("t1")
        assert stats["total_exceptions"] == 2
        assert stats["auto_resolved"] == 1
        assert stats["blocked"] == 1

    def test_store_tenant_isolation(self, store):
        record = store.create(
            tenant_id="t1", order_id="ISO", event_type="T", trace_id=_uid(),
        )
        assert store.get(record.id, "t2") is None
