"""ADR-041 P3e §3.1 — `CaseListItem` schema lock.

Mirror of the asoe-ui architectural lock at
`asoe-ui/tests/architectural/case_summary_projection_fields.test.ts`.

The wire contract for `GET /api/v1/cases` is shared with the UI;
if either side drifts, the projection becomes a partial-truth
state (Guardrail #6). This lock asserts the backend Pydantic
model carries every projection field in the agreed contract.

Bug shape this catches: a backend rename or removal that lands
without coordinating the UI mirror — exactly the drift mechanism
the test-strategy `model_validator` requirement was written to
prevent.
"""

from __future__ import annotations

from api.schemas import CaseListItem, CaseSummaryDollarImpact


REQUIRED_PROJECTION_FIELDS = {
    "customer_name",
    "top_line_sku_code",
    "top_line_sku_title",
    "problem_one_liner",
    "intent",
    "dollar_impact",
    "audit_verdict_color",
}


def test_case_list_item_declares_all_projection_fields():
    model_fields = set(CaseListItem.model_fields.keys())
    missing = REQUIRED_PROJECTION_FIELDS - model_fields
    assert not missing, (
        f"CaseListItem missing CaseSummary projection fields: {missing}. "
        f"ADR-041 P3e §3.1 — backend wire shape must mirror the UI's "
        f"CaseListItem in asoe-ui/src/lib/api.ts."
    )


def test_case_list_item_projection_fields_are_optional():
    """Every projection field must be nullable. The UI's
    EvidenceBlock contract relies on `null` being the legal
    'Structurally Omitted' value; a required field would fail
    Pydantic validation for grandfathered cases."""
    for name in REQUIRED_PROJECTION_FIELDS:
        field = CaseListItem.model_fields[name]
        assert not field.is_required(), (
            f"CaseListItem.{name} must be Optional — "
            "grandfathered cases (no child yet, recipe gap) ship null."
        )


def test_attention_state_is_present_and_constrained():
    """Council 2026-06-07 — the queue groups by `attention_state`.

    Unlike the seven nullable projection fields, this disposition is
    ALWAYS present (every case has a lifecycle state) and constrained
    to the three queue dispositions. Mirror:
    `asoe-ui/tests/architectural/case_summary_projection_fields.test.ts`.
    """
    import pytest

    assert "attention_state" in CaseListItem.model_fields

    for valid in ("NEEDS_HUMAN", "IN_FLIGHT", "DONE"):
        item = CaseListItem(
            case_id="c1",
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            opened_at="2026-05-28T00:00:00Z",
            status="OPEN_AWAITING_HUMAN",
            attention_state=valid,
        )
        assert item.attention_state == valid

    with pytest.raises(Exception):
        CaseListItem(
            case_id="c1",
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            opened_at="2026-05-28T00:00:00Z",
            status="OPEN_AWAITING_HUMAN",
            attention_state="MAYBE",
        )


def test_dollar_impact_shape_requires_amount_cents_and_currency():
    """Compliance Guardrail #6 — a bare amount without currency is
    a partial-truth state. Both fields must be required on the
    shape itself."""
    fields = CaseSummaryDollarImpact.model_fields
    assert fields["amount_cents"].is_required()
    assert fields["currency"].is_required()


def test_dollar_impact_currency_is_iso_4217_length():
    """ISO 4217 codes are exactly 3 letters. The validator must
    reject malformed currencies at the wire boundary so the UI
    never sees a partial code."""
    import pytest

    with pytest.raises(Exception):
        CaseSummaryDollarImpact(amount_cents=100, currency="US")
    with pytest.raises(Exception):
        CaseSummaryDollarImpact(amount_cents=100, currency="USDX")


def test_audit_verdict_color_is_constrained_to_RAG():
    """The shadow-rollup constants are R/A/G plus null. Any other
    value is a contract violation — the UI's `VerdictDot` primitive
    has no rendering for unknown colors and would silently no-op."""
    import pytest

    # Valid values
    for valid in ("R", "A", "G", None):
        item = CaseListItem(
            case_id="c1",
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            opened_at="2026-05-28T00:00:00Z",
            status="OPEN_AWAITING_HUMAN",
            audit_verdict_color=valid,
        )
        assert item.audit_verdict_color == valid

    # Invalid — Pydantic should reject
    with pytest.raises(Exception):
        CaseListItem(
            case_id="c1",
            tenant_id="t1",
            origin="API",
            source_channel="edi_x12_850",
            opened_at="2026-05-28T00:00:00Z",
            status="OPEN_AWAITING_HUMAN",
            audit_verdict_color="UNKNOWN",
        )


def test_case_list_response_items_typed_as_dict_or_caselistitem():
    """The historical CaseListResponse.items is List[Dict[str, Any]]
    (the route serialises dicts for additive-field tolerance). This
    lock guards against an accidental tightening that would reject
    the existing serialiser output."""
    from api.schemas import CaseListResponse

    # Construct with the actual dict shape the serialiser emits
    # — must not raise.
    sample = {
        "case_id": "c1",
        "tenant_id": "t1",
        "origin": "API",
        "source_channel": "edi_x12_850",
        "opened_at": "2026-05-28T00:00:00Z",
        "status": "OPEN_AWAITING_HUMAN",
        "child_intents": [],
        "customer_name": "acme",
        "top_line_sku_code": None,
        "top_line_sku_title": None,
        "problem_one_liner": None,
        "intent": "PRICE_DISCREPANCY",
        "dollar_impact": {"amount_cents": 100, "currency": "USD"},
        "audit_verdict_color": "A",
    }
    response = CaseListResponse(items=[sample], total=1, has_more=False)
    assert response.total == 1
