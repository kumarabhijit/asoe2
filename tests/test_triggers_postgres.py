"""Trigger reject-paths asserted on REAL Postgres.

The `pytest-postgres` job exists because a plpgsql-vs-SQLite divergence
(the V009/V011 `exception_record` bug) was masked by SQLite-only CI. Yet
the DB *trigger* enforcement (V007 metadata contract, V018 supergroup
inheritance, V020 append-only) is asserted only on SQLite, where the
syntax differs (`RAISE(ABORT)` vs `RAISE EXCEPTION`, `json_extract` vs the
`jsonb ?` operator). This locks the reject paths against real Postgres.

Skipped when ASOE_TEST_POSTGRES_URL is unset; runs in pytest-postgres.
"""

from __future__ import annotations

import os
import uuid

import pytest

from db.connection import create_adapter
from db.repository import ExceptionRepository, OrderCaseRepository


def _pg_url() -> str | None:
    return os.getenv("ASOE_TEST_POSTGRES_URL")


pytestmark = pytest.mark.skipif(
    not _pg_url(),
    reason="trigger reject-paths require Postgres (set ASOE_TEST_POSTGRES_URL)",
)


@pytest.fixture(scope="module")
def adapter():
    a = create_adapter(_pg_url())
    a.apply_schema()
    return a


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# V007 — DUPLICATE_PO metadata contract
# ---------------------------------------------------------------------------


def test_v007_duplicate_po_missing_signal_breakdown_rejected(adapter):
    repo = ExceptionRepository(adapter)
    with pytest.raises(Exception) as exc:
        repo.create(
            tenant_id="t-trig", order_id="PO-V007", event_type="T",
            trace_id=_uid(), intent="DUPLICATE_PO",
            final_status="BLOCKED",  # trigger is final_status-scoped (V007)
            resolution_data={"composite_score": 0.9},  # no signal_breakdown
        )
    assert "signal_breakdown" in str(exc.value)


def test_v007_duplicate_po_with_full_contract_accepted(adapter):
    repo = ExceptionRepository(adapter)
    row = repo.create(
        tenant_id="t-trig", order_id="PO-V007-OK", event_type="T",
        trace_id=_uid(), intent="DUPLICATE_PO",
        resolution_data={
            "signal_breakdown": {"po_number": 1.0},
            "composite_score": 0.9,
            "classification": "EXACT_DUPLICATE",
            "recommended_action": "BLOCK_AND_NOTIFY",
        },
    )
    assert row["id"]


# ---------------------------------------------------------------------------
# V020 — case_classification_history is append-only
# ---------------------------------------------------------------------------


def _seed_case_with_history(adapter, tenant_id):
    cases = OrderCaseRepository(adapter)
    case_id = "case-" + uuid.uuid4().hex[:8]
    cases.create(tenant_id, {
        "case_id": case_id, "tenant_id": tenant_id, "origin": "API",
        "source_channel": "edi_x12_850", "supergroup_code": "SG_BLOCK_PRICING",
        "opened_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "status": "OPEN_AGENT_PROCESSING", "tier": 2,
    })
    row_id = _uid()
    with adapter.cursor(tenant_id) as cur:
        cur.execute(
            "INSERT INTO case_classification_history (id, tenant_id, case_id, "
            "supergroup_code, classified_at, classified_by, classifier_type, "
            "taxonomy_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, tenant_id, case_id, "SG_BLOCK_PRICING",
             "2026-01-01T00:00:00Z", "system", "RULE", "v1"),
        )
    return case_id, row_id


def test_v020_classification_history_update_rejected(adapter):
    _, row_id = _seed_case_with_history(adapter, "t-v020-u")
    with pytest.raises(Exception) as exc:
        with adapter.cursor("t-v020-u") as cur:
            cur.execute(
                "UPDATE case_classification_history SET classified_by = 'x' "
                "WHERE id = ?", (row_id,),
            )
    assert "append-only" in str(exc.value).lower()


def test_v020_classification_history_delete_rejected(adapter):
    _, row_id = _seed_case_with_history(adapter, "t-v020-d")
    with pytest.raises(Exception) as exc:
        with adapter.cursor("t-v020-d") as cur:
            cur.execute(
                "DELETE FROM case_classification_history WHERE id = ?", (row_id,),
            )
    assert "append-only" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# V018 — child supergroup inheritance (API child cannot diverge from parent)
# ---------------------------------------------------------------------------


def test_v018_api_child_divergent_supergroup_rejected(adapter):
    tenant_id = "t-v018"
    cases = OrderCaseRepository(adapter)
    exc_repo = ExceptionRepository(adapter)
    case_id = "case-" + uuid.uuid4().hex[:8]
    cases.create(tenant_id, {
        "case_id": case_id, "tenant_id": tenant_id, "origin": "API",
        "source_channel": "edi_x12_850", "supergroup_code": "SG_BLOCK_PRICING",
        "opened_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "status": "OPEN_AGENT_PROCESSING", "tier": 2,
    })
    # Child under an API parent may not carry a different supergroup.
    with pytest.raises(Exception) as exc:
        exc_repo.create(
            tenant_id=tenant_id, order_id="PO-V018", event_type="T",
            trace_id=_uid(), parent_case_id=case_id,
            supergroup_code="SG_BLOCK_CREDIT",  # diverges from parent's PRICING
        )
    assert "diverge" in str(exc.value).lower()


def test_v018_api_child_matching_supergroup_accepted(adapter):
    tenant_id = "t-v018-ok"
    cases = OrderCaseRepository(adapter)
    exc_repo = ExceptionRepository(adapter)
    case_id = "case-" + uuid.uuid4().hex[:8]
    cases.create(tenant_id, {
        "case_id": case_id, "tenant_id": tenant_id, "origin": "API",
        "source_channel": "edi_x12_850", "supergroup_code": "SG_BLOCK_PRICING",
        "opened_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "status": "OPEN_AGENT_PROCESSING", "tier": 2,
    })
    row = exc_repo.create(
        tenant_id=tenant_id, order_id="PO-V018-OK", event_type="T",
        trace_id=_uid(), parent_case_id=case_id,
        supergroup_code="SG_BLOCK_PRICING",  # matches parent
    )
    assert row["id"]
