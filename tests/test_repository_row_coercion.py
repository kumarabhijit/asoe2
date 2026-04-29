"""Regression: ExceptionRepository._to_dict must produce values that pass
pydantic ExceptionSummary / ExceptionDetailResponse validation.

Postgres' psycopg2 returns:
  * UUID columns as ``uuid.UUID`` objects
  * TIMESTAMPTZ columns as ``datetime`` objects

The schema models declare ``id: str``, ``trace_id: str``, ``created_at: str``,
``updated_at: str`` — so the unconverted values triggered
``pydantic_core.ValidationError: 2 validation errors for ExceptionSummary``
on GET /api/v1/exceptions in the live deployment, returning 500 to the UI
and producing the empty exceptions list the operator saw.

This test reproduces the postgres row shape with a hand-built dict-like
object and asserts ``_to_dict`` coerces the four fields to ``str`` so the
downstream pydantic validation succeeds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from api.schemas import ExceptionSummary
from db.repository import ExceptionRepository


# psycopg2 RealDictCursor rows are dict-like (`hasattr(row, "keys")` is True);
# values are the raw driver types.
def _postgres_like_row() -> dict:
    return {
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "tenant_id": "acme-corp",
        "order_id": "PO-12345",
        "event_type": "EDI_850_PRICE_MISMATCH",
        "intent": "CONTRACTUAL_CORRECTION",
        "lifecycle_state": "PENDING_REVIEW",
        "shadow_verdict": "YELLOW",
        "selected_recipe": "PriceAdjustmentRecipe.py",
        "final_status": "MANUAL_REVIEW_REQUIRED",
        "trace_id": UUID("22222222-2222-2222-2222-222222222222"),
        "resolution_data": {},
        "resolved_by": None,
        "resolved_action": None,
        "resolution_notes": None,
        "original_event": {},
        "reanalysis_history": [],
        "enrichment_context": {},
        "created_at": datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 4, 28, 13, 0, 0, tzinfo=timezone.utc),
    }


def test_to_dict_coerces_uuid_to_str():
    repo = ExceptionRepository.__new__(ExceptionRepository)  # bypass __init__
    out = repo._to_dict(_postgres_like_row())
    assert isinstance(out["id"], str)
    assert out["id"] == "11111111-1111-1111-1111-111111111111"
    assert isinstance(out["trace_id"], str)
    assert out["trace_id"] == "22222222-2222-2222-2222-222222222222"


def test_to_dict_coerces_datetime_to_isoformat_str():
    repo = ExceptionRepository.__new__(ExceptionRepository)
    out = repo._to_dict(_postgres_like_row())
    assert isinstance(out["created_at"], str)
    assert isinstance(out["updated_at"], str)
    # ISO 8601 with offset (datetime.isoformat default)
    assert "2026-04-28T12:00:00" in out["created_at"]
    assert "2026-04-28T13:00:00" in out["updated_at"]


def test_to_dict_output_passes_exception_summary_validation():
    """The whole point: row → dict → ExceptionSummary without ValidationError."""
    repo = ExceptionRepository.__new__(ExceptionRepository)
    out = repo._to_dict(_postgres_like_row())
    summary = ExceptionSummary(
        id=out["id"],
        tenant_id=out["tenant_id"],
        order_id=out["order_id"],
        event_type=out["event_type"],
        intent=out.get("intent"),
        lifecycle_state=out["lifecycle_state"],
        shadow_verdict=out.get("shadow_verdict"),
        selected_recipe=out.get("selected_recipe"),
        final_status=out.get("final_status"),
        created_at=out["created_at"],
        updated_at=out["updated_at"],
    )
    assert summary.id == "11111111-1111-1111-1111-111111111111"
    assert summary.created_at.startswith("2026-04-28T12:00:00")


def test_to_dict_passthrough_for_already_string_values():
    """SQLite path returns strings already — coercion must be a no-op."""
    repo = ExceptionRepository.__new__(ExceptionRepository)
    row = _postgres_like_row()
    row["id"] = "33333333-3333-3333-3333-333333333333"
    row["trace_id"] = "44444444-4444-4444-4444-444444444444"
    row["created_at"] = "2026-04-28T15:00:00+00:00"
    row["updated_at"] = "2026-04-28T16:00:00+00:00"
    out = repo._to_dict(row)
    assert out["id"] == "33333333-3333-3333-3333-333333333333"
    assert out["trace_id"] == "44444444-4444-4444-4444-444444444444"
    assert out["created_at"] == "2026-04-28T15:00:00+00:00"
    assert out["updated_at"] == "2026-04-28T16:00:00+00:00"


def test_to_dict_handles_none_uuid_and_timestamp_gracefully():
    """trace_id is NOT NULL in the schema but the coercer must not blow up
    on legacy / partial rows where it might be None."""
    repo = ExceptionRepository.__new__(ExceptionRepository)
    row = _postgres_like_row()
    row["trace_id"] = None
    row["updated_at"] = None
    out = repo._to_dict(row)
    assert out["trace_id"] is None
    assert out["updated_at"] is None
