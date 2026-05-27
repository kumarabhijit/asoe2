"""Phase 2 step 2 — OrderCase carries the new Case & Intent fields.

Covers the additive changes to contracts/models.py:OrderCase:
  - ``origin`` is auto-derived from ``source`` when not passed.
  - The new optional carriers (``supergroup_code``, ``predecessor_case_id``,
    ``will_miss_rdd``, ``sla_due_at``) round-trip through Pydantic.
  - Existing ``case_type`` / ``email_classification`` behaviour is preserved
    (legacy fields stay green through the deprecation window).

CLAUDE.md test-strategy guardrail: this is the focused unit test for the
new ``_default_origin_from_source`` model_validator. Coordinated with
``api/schemas.py`` (which surfaces ``supergroup_code`` / ``intent_code``
on responses) via ``tests/test_case_api_surfaces_new_fields.py``.
"""

from __future__ import annotations

from contracts.models import OrderCase, infer_origin


# ---------------------------------------------------------------------------
# Pure mapping
# ---------------------------------------------------------------------------

def test_infer_origin_manual_order():
    assert infer_origin("manual_order") == "CUSTOMER"


def test_infer_origin_automated_order():
    assert infer_origin("automated_order") == "API"


def test_infer_origin_unknown_defaults_to_api():
    """Conservative default — anything we don't recognise is treated as
    system-initiated rather than customer-initiated."""
    assert infer_origin("future_source_value") == "API"


# ---------------------------------------------------------------------------
# OrderCase auto-derivation
# ---------------------------------------------------------------------------

def test_origin_auto_derived_from_manual_source():
    case = OrderCase(
        tenant_id="t1", source="manual_order", source_channel="email",
    )
    assert case.origin == "CUSTOMER"
    # Legacy fields are still populated correctly by the existing validator.
    assert case.case_type == "EMAIL_ENTRY"
    assert case.email_classification == "OTHER"


def test_origin_auto_derived_from_automated_source():
    case = OrderCase(
        tenant_id="t1", source="automated_order", source_channel="edi_x12_850",
    )
    assert case.origin == "API"
    assert case.case_type == "BLOCK"
    assert case.email_classification is None


def test_explicit_origin_overrides_derivation():
    """An explicit ``origin`` is respected — the validator only fills the
    field when it is None, so callers retain full control."""
    case = OrderCase(
        tenant_id="t1", source="manual_order", source_channel="email",
        origin="API",
    )
    assert case.origin == "API"


# ---------------------------------------------------------------------------
# New optional carriers
# ---------------------------------------------------------------------------

def test_new_carriers_default_to_unset():
    case = OrderCase(
        tenant_id="t1", source="manual_order", source_channel="email",
    )
    assert case.supergroup_code is None
    assert case.predecessor_case_id is None
    assert case.will_miss_rdd is False
    assert case.sla_due_at is None


def test_new_carriers_accept_values():
    case = OrderCase(
        tenant_id="t1", source="automated_order", source_channel="edi_x12_850",
        supergroup_code="SG_BLOCK_PRICING",
        predecessor_case_id="case-001",
        will_miss_rdd=True,
        sla_due_at="2026-05-28T12:00:00+00:00",
    )
    assert case.supergroup_code == "SG_BLOCK_PRICING"
    assert case.predecessor_case_id == "case-001"
    assert case.will_miss_rdd is True
    assert case.sla_due_at == "2026-05-28T12:00:00+00:00"


def test_origin_derivation_runs_before_case_type_derivation():
    """Both pre-validators fire on construction; new origin doesn't
    interfere with the existing case_type / email_classification chain."""
    case = OrderCase(
        tenant_id="t1", source="manual_order", source_channel="email",
    )
    assert case.origin == "CUSTOMER"
    assert case.case_type == "EMAIL_ENTRY"
    assert case.email_classification == "OTHER"
