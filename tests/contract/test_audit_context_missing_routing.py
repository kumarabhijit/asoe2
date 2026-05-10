"""Spec-as-oracle: AUDIT_CONTEXT_MISSING terminal status routing.

Per `contracts/models.py::STATUS_TO_LIFECYCLE`:
  AUDIT_CONTEXT_MISSING → FAILED

Per the Verdict 2026-04-22 workshop (CLAUDE.md §6) the
AUDIT_CONTEXT_MISSING terminal status:
  1. Must be in `TerminalStatus` enum.
  2. Must map to FAILED lifecycle (no reviewer path — the record
     cannot be audited).
  3. Must be exposed via `/api/v1/health.lifecycle_states` and
     reachable via the build_analysis composer when an audit-bearing
     field is missing.

Reference: docs/test-strategy/eng-review-test-plan.md (Key Interactions
§4 — "AUDIT_CONTEXT_MISSING terminal status → UI renders an explicit
placeholder section").
"""

from __future__ import annotations

import pytest

from contracts.models import STATUS_TO_LIFECYCLE, TerminalStatus


def test_audit_context_missing_in_terminal_status_enum() -> None:
    """The enum value exists. Trivial but anchors the parity tests."""
    assert TerminalStatus.AUDIT_CONTEXT_MISSING.value == "AUDIT_CONTEXT_MISSING"


def test_audit_context_missing_routes_to_failed() -> None:
    """Per Verdict: the record cannot be audited so it cannot be
    sent for human review (PENDING_REVIEW would imply a reviewable
    payload exists). Lifecycle is FAILED."""
    assert STATUS_TO_LIFECYCLE["AUDIT_CONTEXT_MISSING"] == "FAILED"


def test_every_terminal_status_has_lifecycle_mapping() -> None:
    """Every TerminalStatus enum value must have a row in
    STATUS_TO_LIFECYCLE; otherwise the executor crashes on a status
    it has no lifecycle for."""
    missing = [
        status.value for status in TerminalStatus
        if status.value not in STATUS_TO_LIFECYCLE
    ]
    assert not missing, (
        f"TerminalStatus values without a STATUS_TO_LIFECYCLE entry: "
        f"{missing}"
    )


def test_status_to_lifecycle_keys_are_real_terminal_statuses() -> None:
    """Inverse: no orphaned routing entries."""
    real_values = {s.value for s in TerminalStatus}
    extra = set(STATUS_TO_LIFECYCLE) - real_values
    assert not extra, (
        f"STATUS_TO_LIFECYCLE has keys not in TerminalStatus: "
        f"{sorted(extra)}"
    )


def test_health_endpoint_advertises_audit_context_missing() -> None:
    """The /api/v1/health endpoint exports the lifecycle_states list
    that asoe-ui consumes to render badges. AUDIT_CONTEXT_MISSING is
    a terminal status not a lifecycle state — but the FAILED lifecycle
    it routes to MUST be in lifecycle_states."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api.app import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    res = client.get("/api/v1/health")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "FAILED" in body["lifecycle_states"], (
        "FAILED lifecycle is missing from /health.lifecycle_states. "
        "AUDIT_CONTEXT_MISSING records route here; the UI cannot "
        "render the placeholder section without it."
    )
