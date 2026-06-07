"""Council 2026-06-07 — `attention_state` disposition unit tests.

The /cases queue separates needs-human work from already-done /
in-flight work by grouping on a backend-owned `attention_state`
(Reis ruling). These tests lock:

  * the lifecycle -> disposition mapping is exhaustive over the
    canonical `CaseStatus` enum (a new state can't silently inherit
    the NEEDS_HUMAN default unnoticed);
  * the deterministic mapping itself;
  * the unknown-state fallback (NEEDS_HUMAN — never silently bury);
  * the field is projected onto `CaseSummary` + its wire dict.
"""

from __future__ import annotations

from typing import get_args

import pytest

from contracts.models import CaseStatus
from api.case_summary import (
    _ATTENTION_BY_STATUS,
    _attention_of,
    compute_case_summary,
)
from api.store import case_store, exception_store


@pytest.fixture()
def cleanup():
    case_store.clear()
    exception_store.clear()
    yield
    case_store.clear()
    exception_store.clear()


def test_mapping_is_exhaustive_over_case_status():
    """Every canonical CaseStatus has an explicit disposition.

    Fails if a lifecycle state is added to `contracts.models.CaseStatus`
    without a deliberate row in `_ATTENTION_BY_STATUS`, forcing the
    author to make the needs-human call rather than inherit the
    fallback by accident.
    """
    assert set(_ATTENTION_BY_STATUS) == set(get_args(CaseStatus))


@pytest.mark.parametrize(
    "status,expected",
    [
        ("OPEN_AWAITING_HUMAN", "NEEDS_HUMAN"),
        ("FAILED", "NEEDS_HUMAN"),
        ("BLOCKED", "NEEDS_HUMAN"),
        ("OPEN_AGENT_PROCESSING", "IN_FLIGHT"),
        ("OPEN_AWAITING_BUYER", "IN_FLIGHT"),
        ("OPEN_AWAITING_ERP", "IN_FLIGHT"),
        ("RESOLVED", "DONE"),
    ],
)
def test_attention_of_maps_each_state(status, expected):
    class _Case:
        pass

    case = _Case()
    case.status = status
    assert _attention_of(case) == expected


def test_unknown_state_defaults_to_needs_human():
    class _Case:
        status = "SOME_FUTURE_STATE"

    assert _attention_of(_Case()) == "NEEDS_HUMAN"

    class _NoStatus:
        pass

    assert _attention_of(_NoStatus()) == "NEEDS_HUMAN"


def test_compute_case_summary_projects_attention_state(cleanup):
    case, _ = case_store.lookup_or_create(
        "tenant-a",
        origin="API",
        source_channel="edi_x12_850",
        customer_po_number="PO-ATT-1",
    )
    summary = compute_case_summary(case, [])
    # Freshly-opened case → agent processing → IN_FLIGHT.
    assert summary.attention_state == "IN_FLIGHT"
    assert summary.to_dict()["attention_state"] == "IN_FLIGHT"
