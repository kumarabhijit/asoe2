"""ADR-041 P3e §3.1 — `case_summary.compute_case_summary` unit tests.

Covers the verdict rollup (severity wins: R > A > G), the
primary-record pick (highest financial_impact_usd), the
dollar_impact USD→cents conversion + zero/None guards, and the
explicit-null defaults for the per-intent template fields the
recipe team owns (top_line_sku_*, problem_one_liner).
"""

from __future__ import annotations

import pytest

from api.case_summary import (
    CaseSummary,
    DollarImpact,
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


def _open_case(**kwargs):
    case, _ = case_store.lookup_or_create(
        kwargs.pop("tenant_id", "tenant-a"),
        origin=kwargs.pop("origin", "API"),
        source_channel=kwargs.pop("source_channel", "edi_x12_850"),
        **kwargs,
    )
    return case


def _attach_child(
    *,
    parent_case_id: str,
    order_id: str = "PO-1",
    tenant_id: str = "tenant-a",
    shadow_verdict: str | None = None,
    intent: str | None = None,
    resolution_data: dict | None = None,
):
    record = exception_store.create(
        tenant_id=tenant_id,
        order_id=order_id,
        event_type="EDI_850",
        trace_id=f"trace-{order_id}",
        intent=intent,
        shadow_verdict=shadow_verdict,
        resolution_data=resolution_data or {},
        parent_case_id=parent_case_id,
    )
    return record


# ---------------------------------------------------------------------------
# customer_name + null projections
# ---------------------------------------------------------------------------


def test_customer_name_projects_from_customer_id(cleanup):
    case = _open_case(customer_po_number="PO-X", customer_id="acme-corp")
    summary = compute_case_summary(case, [])
    assert summary.customer_name == "acme-corp"


def test_customer_name_null_when_customer_id_absent(cleanup):
    case = _open_case(customer_po_number="PO-X")
    summary = compute_case_summary(case, [])
    assert summary.customer_name is None


def test_per_intent_template_fields_default_to_none(cleanup):
    """Recipe team owns top_line_sku_* and problem_one_liner; the
    projection MUST NOT synthesise these from event metadata."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        intent="PRICE_DISCREPANCY",
        shadow_verdict="YELLOW",
        resolution_data={"financial_impact_usd": 100.0},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    summary = compute_case_summary(case, records)
    assert summary.top_line_sku_code is None
    assert summary.top_line_sku_title is None
    assert summary.problem_one_liner is None


# ---------------------------------------------------------------------------
# verdict rollup
# ---------------------------------------------------------------------------


def test_verdict_color_none_when_no_records(cleanup):
    case = _open_case(customer_po_number="PO-X")
    summary = compute_case_summary(case, [])
    assert summary.audit_verdict_color is None


def test_verdict_color_none_when_records_have_no_verdict(cleanup):
    case = _open_case(customer_po_number="PO-X")
    _attach_child(parent_case_id=case.case_id, shadow_verdict=None)
    records = exception_store.list_by_case("tenant-a", case.case_id)
    summary = compute_case_summary(case, records)
    assert summary.audit_verdict_color is None


def test_verdict_color_green_when_all_green(cleanup):
    case = _open_case(customer_po_number="PO-X")
    _attach_child(parent_case_id=case.case_id, order_id="PO-1", shadow_verdict="GREEN")
    _attach_child(parent_case_id=case.case_id, order_id="PO-2", shadow_verdict="GREEN")
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).audit_verdict_color == "G"


def test_verdict_color_amber_when_any_yellow(cleanup):
    case = _open_case(customer_po_number="PO-X")
    _attach_child(parent_case_id=case.case_id, order_id="PO-1", shadow_verdict="GREEN")
    _attach_child(parent_case_id=case.case_id, order_id="PO-2", shadow_verdict="YELLOW")
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).audit_verdict_color == "A"


def test_verdict_color_red_dominates(cleanup):
    """Severity-wins rollup: a single RED child trumps GREEN + YELLOW."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(parent_case_id=case.case_id, order_id="PO-1", shadow_verdict="GREEN")
    _attach_child(parent_case_id=case.case_id, order_id="PO-2", shadow_verdict="YELLOW")
    _attach_child(parent_case_id=case.case_id, order_id="PO-3", shadow_verdict="RED")
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).audit_verdict_color == "R"


# ---------------------------------------------------------------------------
# intent + primary-record pick
# ---------------------------------------------------------------------------


def test_intent_from_first_record_when_no_impact(cleanup):
    case = _open_case(customer_po_number="PO-X")
    _attach_child(parent_case_id=case.case_id, order_id="PO-1", intent="DUPLICATE_PO")
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).intent == "DUPLICATE_PO"


def test_intent_picks_highest_dollar_child(cleanup):
    """Recipe SME §4 — the primary line is the one driving triage
    by dollar value, not insertion order."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        order_id="PO-1",
        intent="MOQ_UPLIFT",
        resolution_data={"financial_impact_usd": 50.0},
    )
    _attach_child(
        parent_case_id=case.case_id,
        order_id="PO-2",
        intent="PRICE_DISCREPANCY",
        resolution_data={"financial_impact_usd": 5000.0},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).intent == "PRICE_DISCREPANCY"


def test_intent_none_when_no_records(cleanup):
    case = _open_case(customer_po_number="PO-X")
    assert compute_case_summary(case, []).intent is None


# ---------------------------------------------------------------------------
# dollar_impact
# ---------------------------------------------------------------------------


def test_dollar_impact_converts_dollars_to_cents(cleanup):
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        intent="PRICE_DISCREPANCY",
        resolution_data={"financial_impact_usd": 41.47},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    summary = compute_case_summary(case, records)
    assert summary.dollar_impact == DollarImpact(amount_cents=4147, currency="USD")


def test_dollar_impact_rounds_to_nearest_cent(cleanup):
    """Sub-cent amounts must round to the nearest cent — straight
    `int()` cast truncates, which silently underpays. Banker's
    rounding (Python `round()`) is the financial-systems standard;
    we verify the rounding behaves on an unambiguously-above-half
    case so the test is float-representation safe."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        intent="PRICE_DISCREPANCY",
        resolution_data={"financial_impact_usd": 0.006},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    summary = compute_case_summary(case, records)
    assert summary.dollar_impact is not None
    assert summary.dollar_impact.amount_cents == 1  # 0.6¢ → 1¢, not 0


def test_dollar_impact_null_when_absent(cleanup):
    case = _open_case(customer_po_number="PO-X")
    _attach_child(parent_case_id=case.case_id, intent="EDI_MISMATCH")
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).dollar_impact is None


def test_dollar_impact_null_when_zero(cleanup):
    """Zero-impact rows collapse the cell — not actionable in the
    dollar-sort triage frame."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        resolution_data={"financial_impact_usd": 0.0},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).dollar_impact is None


def test_dollar_impact_null_for_non_numeric_value(cleanup):
    """Partial-truth guard — a string in financial_impact_usd is
    treated as absence, not parsed."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        resolution_data={"financial_impact_usd": "not a number"},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).dollar_impact is None


# ---------------------------------------------------------------------------
# ADR-041 P3e Phase 0b T2d — multi-currency carrier
# ---------------------------------------------------------------------------


def test_dollar_impact_picks_up_explicit_currency_carrier(cleanup):
    """Preferred path — `financial_impact` + `currency` fields
    project directly. Currency stays as written (not coerced to
    USD)."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        intent="PRICE_DISCREPANCY",
        resolution_data={"financial_impact": 1234.50, "currency": "EUR"},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    summary = compute_case_summary(case, records)
    assert summary.dollar_impact == DollarImpact(
        amount_cents=123450, currency="EUR",
    )


def test_dollar_impact_preferred_wins_over_legacy(cleanup):
    """When both shapes are present (recipe mid-migration), the
    explicit-currency one wins. This lets recipes land
    multi-currency support without immediately deleting the legacy
    `_usd` field."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        resolution_data={
            "financial_impact": 100.0,
            "currency": "GBP",
            "financial_impact_usd": 99.0,  # stale legacy
        },
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    summary = compute_case_summary(case, records)
    assert summary.dollar_impact is not None
    assert summary.dollar_impact.currency == "GBP"
    assert summary.dollar_impact.amount_cents == 10000


def test_dollar_impact_currency_normalised_to_uppercase(cleanup):
    """ISO 4217 codes are uppercase; lowercase / mixed input
    normalises so the wire shape is consistent."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        resolution_data={"financial_impact": 50.0, "currency": "jpy"},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    summary = compute_case_summary(case, records)
    assert summary.dollar_impact is not None
    assert summary.dollar_impact.currency == "JPY"


def test_dollar_impact_rejects_malformed_currency(cleanup):
    """Two-letter or four-letter currency codes are not ISO 4217.
    Reject at the projection boundary so the UI never sees a
    partial-truth code."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        resolution_data={"financial_impact": 100, "currency": "US"},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).dollar_impact is None


def test_dollar_impact_rejects_non_alpha_currency(cleanup):
    """Currency code containing digits or punctuation — defensive
    rejection so corrupted/malicious data can't smuggle through."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        resolution_data={"financial_impact": 100, "currency": "U$D"},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).dollar_impact is None


def test_dollar_impact_explicit_currency_only_no_amount_returns_none(cleanup):
    """Currency without an amount is missing data; nothing to
    render."""
    case = _open_case(customer_po_number="PO-X")
    _attach_child(
        parent_case_id=case.case_id,
        resolution_data={"currency": "EUR"},
    )
    records = exception_store.list_by_case("tenant-a", case.case_id)
    assert compute_case_summary(case, records).dollar_impact is None


# ---------------------------------------------------------------------------
# to_dict — wire shape contract
# ---------------------------------------------------------------------------


def test_summary_to_dict_wire_shape():
    summary = CaseSummary(
        customer_name="acme-corp",
        top_line_sku_code=None,
        top_line_sku_title=None,
        problem_one_liner=None,
        intent="PRICE_DISCREPANCY",
        dollar_impact=DollarImpact(amount_cents=4147, currency="USD"),
        audit_verdict_color="R",
        attention_state="NEEDS_HUMAN",
    )
    assert summary.to_dict() == {
        "customer_name": "acme-corp",
        "top_line_sku_code": None,
        "top_line_sku_title": None,
        "problem_one_liner": None,
        "intent": "PRICE_DISCREPANCY",
        "dollar_impact": {"amount_cents": 4147, "currency": "USD"},
        "audit_verdict_color": "R",
        "attention_state": "NEEDS_HUMAN",
    }


def test_summary_to_dict_null_dollar_impact_becomes_none():
    """The wire shape distinguishes `null dollar_impact` from
    `dict without amount` — the UI's EvidenceBlock relies on the
    field being `null`, not `{}`."""
    summary = CaseSummary(
        customer_name=None,
        top_line_sku_code=None,
        top_line_sku_title=None,
        problem_one_liner=None,
        intent=None,
        dollar_impact=None,
        audit_verdict_color=None,
        attention_state="DONE",
    )
    assert summary.to_dict()["dollar_impact"] is None
