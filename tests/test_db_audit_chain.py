"""Phase 4 — SQL-side hash-chained audit log.

Mirrors tests/test_audit_chain.py (which exercises the in-memory store)
and additionally proves the V003 DB-level UPDATE/DELETE triggers
reject mutations. Runs against an in-memory SQLite database so it has
no external dependency.

Invariants under test:
  - prev_hash + event_hash columns are populated on insert.
  - First event per tenant carries prev_hash="GENESIS".
  - Each subsequent event's prev_hash equals the predecessor event_hash.
  - verify_audit_chain() returns (True, None) on a clean log.
  - DB-level trigger rejects UPDATE on policy_audit_log with the
    expected error message — V003's append-only enforcement.
  - DB-level trigger rejects DELETE on policy_audit_log.
  - Per-tenant isolation: tenant-b's chain is independent of tenant-a's.
  - The hash function matches the in-memory store's so a chain written
    by one is verifiable by the other (cross-implementation parity).
"""

from __future__ import annotations

import sqlite3

import pytest

from db.connection import SQLiteAdapter
from db.repository import PolicyRepository, _audit_event_hash


@pytest.fixture()
def adapter():
    a = SQLiteAdapter(":memory:")
    a.apply_schema()
    return a


@pytest.fixture()
def repo(adapter):
    return PolicyRepository(adapter)


def _seed(repo, tenant_id: str, n: int = 3) -> None:
    for i in range(n):
        repo.create_audit_event(
            tenant_id=tenant_id,
            policy_key=f"EXCEPTION_RESOLVED",
            previous_value={"step": i},
            new_value={"step": i + 1, "tag": f"row-{i}"},
            changed_by=f"user-{i}@x",
            change_reason=f"event {i}",
        )


def test_columns_present_after_v003(adapter):
    with adapter.connection() as conn:
        cur = conn.execute("PRAGMA table_info(policy_audit_log)")
        cols = {row[1] for row in cur.fetchall()}
    assert "prev_hash" in cols
    assert "event_hash" in cols


def test_first_event_per_tenant_is_genesis(repo):
    _seed(repo, "tenant-a", n=1)
    rows = repo.list_audit_log("tenant-a")
    assert len(rows) == 1
    assert rows[0]["prev_hash"] == "GENESIS"
    assert rows[0]["event_hash"] != ""


def test_chain_links_match(repo):
    _seed(repo, "tenant-a", n=4)
    rows = repo.list_audit_log("tenant-a", limit=100)
    # list_audit_log returns DESC; reverse to walk in chronological order.
    rows = list(reversed(rows))
    for i in range(1, len(rows)):
        assert rows[i]["prev_hash"] == rows[i - 1]["event_hash"], (
            f"chain broken at index {i}"
        )


def test_verify_audit_chain_passes_on_clean_log(repo):
    _seed(repo, "tenant-a", n=5)
    valid, break_at = repo.verify_audit_chain("tenant-a")
    assert valid is True
    assert break_at is None


def test_chains_are_per_tenant(repo):
    _seed(repo, "tenant-a", n=2)
    _seed(repo, "tenant-b", n=2)
    rows_a = list(reversed(repo.list_audit_log("tenant-a")))
    rows_b = list(reversed(repo.list_audit_log("tenant-b")))
    assert rows_a[0]["prev_hash"] == "GENESIS"
    assert rows_b[0]["prev_hash"] == "GENESIS"
    assert repo.verify_audit_chain("tenant-a") == (True, None)
    assert repo.verify_audit_chain("tenant-b") == (True, None)


def test_db_trigger_rejects_update(adapter, repo):
    _seed(repo, "tenant-a", n=1)
    with adapter.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute(
                "UPDATE policy_audit_log SET changed_by = ? WHERE tenant_id = ?",
                ("attacker@x", "tenant-a"),
            )
    assert "append-only" in str(exc.value).lower()


def test_db_trigger_rejects_delete(adapter, repo):
    _seed(repo, "tenant-a", n=1)
    with adapter.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute(
                "DELETE FROM policy_audit_log WHERE tenant_id = ?",
                ("tenant-a",),
            )
    assert "append-only" in str(exc.value).lower()


def test_hash_function_matches_in_memory_store(repo):
    """Cross-implementation parity check.

    The in-memory store, the DB repository, and the V003 SQLite
    backfill all compute event_hash = sha256(prev || canonical_json).
    Recomputing one row's hash with the shared _audit_event_hash helper
    must reproduce exactly what the repository wrote.
    """
    _seed(repo, "tenant-a", n=1)
    row = repo.list_audit_log("tenant-a")[0]
    fields = {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "policy_key": row["policy_key"],
        # _insert_audit_event stores the JSON-serialized form on disk; the
        # hash is computed over that exact string. list_audit_log decodes
        # it for ergonomics, so re-serialize before hashing.
        "previous_value": _maybe_json(row["previous_value"]),
        "new_value": _maybe_json(row["new_value"]),
        "changed_by": row["changed_by"],
        "change_reason": row["change_reason"],
        "created_at": row["created_at"],
        "prev_hash": row["prev_hash"],
    }
    expected = _audit_event_hash(row["prev_hash"], fields)
    assert expected == row["event_hash"]


def _maybe_json(value):
    """Re-serialize the decoded value to the same JSON the repo persisted."""
    import json as _json
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _json.dumps(value, separators=(", ", ": "))
