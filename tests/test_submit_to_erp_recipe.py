"""ADR-042 Phase 3 — SubmitToErpRecipe (deterministic ERP sales-order build).

The financially-binding order-entry submit. Pure, deterministic logic
(CLAUDE.md Guardrail #1): given the operator-reviewed extracted order plus any
operator corrections, build the SAP sales-order-create (BAPI) payload, apply
the corrections with a before/after audit, and validate submittability. No I/O
and no gateway calls here — the orchestration layer applies the ERP write as a
gateway effect after Compliance Shadow (and cosign >$10k) clear.

Written test-first.
"""

from __future__ import annotations

from recipes.SubmitToErpRecipe import build_erp_submission

_HEADER = {
    "customer_po": "0093847612",
    "order_type": "ZOR",
    "sales_org": "1000",
    "dist_channel": "10",
    "requested_date": "2025-03-17",
}

_LINES = [
    {"line_num": "001", "material": "BEV-COLA-12PK", "quantity": 480,
     "uom": "CS", "unit_price": 8.64},
    {"line_num": "002", "material": "BEV-LEMON-12PK", "quantity": 120,
     "uom": "CS", "unit_price": 9.10},
]


def _submit(**overrides):
    base = dict(
        order_id="SO-1", customer_bp="300001",
        header=dict(_HEADER), line_items=[dict(li) for li in _LINES],
        corrections=None,
    )
    base.update(overrides)
    return build_erp_submission(**base)


def test_clean_order_builds_success_payload() -> None:
    out = _submit()
    assert out["status"] == "SUCCESS"
    assert out["reason"] is None
    payload = out["erp_payload"]
    assert payload["sales_org"] == "1000"
    assert payload["dist_channel"] == "10"
    assert payload["customer_bp"] == "300001"
    assert payload["customer_po"] == "0093847612"
    assert len(payload["line_items"]) == 2
    assert out["line_count"] == 2
    # total = 480*8.64 + 120*9.10
    assert out["total_value_usd"] == round(480 * 8.64 + 120 * 9.10, 2)
    assert out["corrections_applied"] == []


def test_line_quantity_correction_is_applied_and_audited() -> None:
    out = _submit(corrections={"lines": {"001": {"quantity": 500}}})
    assert out["status"] == "SUCCESS"
    line = next(li for li in out["erp_payload"]["line_items"] if li["line_num"] == "001")
    assert line["quantity"] == 500
    # before/after audit recorded for the changed field.
    audit = out["corrections_applied"]
    assert {"line_num": "001", "field": "quantity", "before": 480, "after": 500} in audit
    # total reflects the corrected quantity.
    assert out["total_value_usd"] == round(500 * 8.64 + 120 * 9.10, 2)


def test_header_correction_is_applied_and_audited() -> None:
    out = _submit(corrections={"header": {"requested_date": "2025-04-01"}})
    assert out["erp_payload"]["requested_date"] == "2025-04-01"
    assert {"field": "requested_date", "before": "2025-03-17", "after": "2025-04-01"} \
        in out["corrections_applied"]


def test_no_line_items_is_rejected() -> None:
    out = _submit(line_items=[])
    assert out["status"] == "REJECTED"
    assert out["reason"]


def test_line_missing_material_is_rejected() -> None:
    out = _submit(line_items=[
        {"line_num": "001", "material": "", "quantity": 10, "uom": "CS", "unit_price": 1.0},
    ])
    assert out["status"] == "REJECTED"


def test_line_non_positive_quantity_is_rejected() -> None:
    out = _submit(line_items=[
        {"line_num": "001", "material": "M", "quantity": 0, "uom": "CS", "unit_price": 1.0},
    ])
    assert out["status"] == "REJECTED"


def test_correction_to_unknown_line_is_ignored_not_crashing() -> None:
    # A correction targeting a line that isn't present must not crash or
    # fabricate a line; it is simply not applied.
    out = _submit(corrections={"lines": {"999": {"quantity": 5}}})
    assert out["status"] == "SUCCESS"
    assert all(c["line_num"] != "999" for c in out["corrections_applied"]
               if "line_num" in c)
