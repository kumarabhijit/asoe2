"""OrderCase carries the Case & Intent Super-Group fields (requirements §5).

After the Phase 2 cleanup (commit 6), ``origin`` is a required field on
OrderCase — no source-based derivation, no legacy ``source`` field. This
test locks the new contract.
"""

from __future__ import annotations

import pytest

from contracts.models import OrderCase


# ---------------------------------------------------------------------------
# Required-field shape
# ---------------------------------------------------------------------------

def test_customer_case_constructs():
    case = OrderCase(
        tenant_id="t1", origin="CUSTOMER", source_channel="email",
    )
    assert case.origin == "CUSTOMER"
    assert case.source_channel == "email"


def test_api_case_constructs():
    case = OrderCase(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
    )
    assert case.origin == "API"


def test_origin_is_required():
    """``origin`` has no default and no derivation — callers must pass it."""
    with pytest.raises(Exception):  # ValidationError
        OrderCase(tenant_id="t1", source_channel="email")  # type: ignore[call-arg]


def test_invalid_origin_rejected():
    with pytest.raises(Exception):  # ValidationError
        OrderCase(  # type: ignore[arg-type]
            tenant_id="t1", origin="BOTH", source_channel="email",
        )


# ---------------------------------------------------------------------------
# New optional carriers (requirements §5)
# ---------------------------------------------------------------------------

def test_new_carriers_default_to_unset():
    case = OrderCase(
        tenant_id="t1", origin="CUSTOMER", source_channel="email",
    )
    assert case.supergroup_code is None
    assert case.predecessor_case_id is None
    assert case.will_miss_rdd is False
    assert case.sla_due_at is None


def test_new_carriers_accept_values():
    case = OrderCase(
        tenant_id="t1", origin="API", source_channel="edi_x12_850",
        supergroup_code="SG_BLOCK_PRICING",
        predecessor_case_id="case-001",
        will_miss_rdd=True,
        sla_due_at="2026-05-28T12:00:00+00:00",
    )
    assert case.supergroup_code == "SG_BLOCK_PRICING"
    assert case.predecessor_case_id == "case-001"
    assert case.will_miss_rdd is True
    assert case.sla_due_at == "2026-05-28T12:00:00+00:00"


def test_tier_accepts_new_value_4():
    """Requirements §8.4 SLA bucketing introduces tier 4 (>24h SLA)."""
    case = OrderCase(
        tenant_id="t1", origin="API", source_channel="edi_x12_850", tier=4,
    )
    assert case.tier == 4


def test_tier_rejects_out_of_range():
    with pytest.raises(Exception):  # ValidationError
        OrderCase(
            tenant_id="t1", origin="API", source_channel="edi_x12_850",
            tier=5,  # type: ignore[arg-type]
        )


def test_extra_fields_forbidden():
    """``extra='forbid'`` on the model — typos raise at construction rather
    than silently dropping. Uses a generic sentinel so the assertion does
    not rot if a real future field happens to share the name of a long-dead
    one."""
    with pytest.raises(Exception):  # ValidationError
        OrderCase(  # type: ignore[call-arg]
            tenant_id="t1", origin="CUSTOMER", source_channel="email",
            unknown_field_should_raise="x",
        )
