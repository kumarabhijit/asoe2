"""Regression (A1) — `ReanalysisHistoryEntry` field names must mirror the
wire payload built by the reanalyze endpoint and consumed by the UI.

The reanalyze route (api/routes/exceptions.py::reanalyze) hand-builds the
history entry dict with `triggered_at` / `triggered_by`, and the UI's
`ReanalysisEntry` reads the same keys. The typed model previously declared
`attempted_at` / `attempted_by`, which silently disagreed with the wire —
the untyped `reanalysis_history: List[Dict[str, Any]]` field hid the drift
from validation.

With `model_config = ConfigDict(extra="forbid")`, validating the real wire
dict against the model is an exact bidirectional lock: it fails on the
parent commit (unexpected `triggered_*` keys + missing required
`attempted_*`) and passes once the field names match.
"""

from __future__ import annotations

from api.schemas import ReanalysisHistoryEntry


def _wire_entry() -> dict:
    """Mirror the prior_entry dict constructed in
    api/routes/exceptions.py::reanalyze (the authoritative wire shape)."""
    return {
        "attempt": 1,
        "triggered_at": "2026-06-14T00:00:00+00:00",
        "triggered_by": "manager@tenant-a",
        "reason": "Buyer sent corrected PO",
        "prior_trace_id": "tr-old",
        "prior_shadow_verdict": "YELLOW",
        "prior_final_status": "MANUAL_REVIEW_REQUIRED",
        "prior_lifecycle_state": "PENDING_REVIEW",
        "new_trace_id": "tr-new",
        "new_shadow_verdict": "GREEN",
        "new_final_status": "COMPLETE",
        "new_lifecycle_state": "RESOLVED",
        "executed_nodes": [],
    }


def test_model_validates_against_wire_dict():
    entry = ReanalysisHistoryEntry(**_wire_entry())
    assert entry.attempt == 1
    assert entry.triggered_at == "2026-06-14T00:00:00+00:00"
    assert entry.triggered_by == "manager@tenant-a"


def test_field_names_match_wire_not_legacy():
    fields = set(ReanalysisHistoryEntry.model_fields)
    assert "triggered_at" in fields
    assert "triggered_by" in fields
    # The legacy names must be gone, or the untyped history dict and the
    # typed model would diverge again.
    assert "attempted_at" not in fields
    assert "attempted_by" not in fields
