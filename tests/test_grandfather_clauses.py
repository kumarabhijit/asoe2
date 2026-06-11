"""Grandfather-clause structure + enforcement tests.

Regression context (Phase 3 contract audit, 2026-06-11): the
`grandfather_clauses` section of compliance/audit_bearing_registry.yaml
was prose comments only, so it parsed as an EMPTY mapping.
`api/analysis_composer.py::_load_grandfathered_fields` therefore always
returned an empty waived-set, and the documented "clauses expire
automatically at deadline" mechanism was dead code. No record misrouted
*today* only because the affected fields are parked `tier: contextual`
— but the first reclassification to audit-bearing that relied on a
clause would have routed records to AUDIT_CONTEXT_MISSING immediately
instead of after the compliance-approved deadline.

These tests pin:
  * the section parses to structured entries (deadline date + fields),
  * every waived dotted name references a real registry row,
  * the reader honors deadlines (waived before, dropped after),
  * restoring the structure does NOT change today's enforcement for
    the currently-contextual waived fields (no behavior change).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from api.analysis_composer import _load_grandfathered_fields, _REGISTRY

REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "compliance"
    / "audit_bearing_registry.yaml"
)


def _clauses() -> dict:
    with REGISTRY_PATH.open() as fh:
        registry = yaml.safe_load(fh)
    return registry.get("grandfather_clauses") or {}


def test_grandfather_clauses_parse_as_structured_entries():
    """REGRESSION — fails on the parent commit, where the section body
    was comments only and parsed as None/empty."""
    clauses = _clauses()
    assert isinstance(clauses, dict) and clauses, (
        "grandfather_clauses must contain structured entries; prose "
        "comments alone parse to an empty mapping and disable the "
        "deadline-waiver mechanism entirely"
    )
    for name, clause in clauses.items():
        assert isinstance(clause, dict), f"{name} must be a mapping"
        assert isinstance(clause.get("deadline"), date), (
            f"{name}.deadline must be an ISO date (PyYAML parses it to "
            "datetime.date, which _load_grandfathered_fields requires)"
        )
        assert isinstance(clause.get("fields"), list), (
            f"{name}.fields must be a list (possibly empty for "
            "provenance-tracking clauses)"
        )


def test_every_waived_field_references_a_real_registry_row():
    for name, clause in _clauses().items():
        for dotted in clause["fields"]:
            section_name, _, field_name = dotted.partition(".")
            section = _REGISTRY.get(section_name)
            assert isinstance(section, dict), (
                f"{name} waives '{dotted}' but registry section "
                f"'{section_name}' does not exist"
            )
            assert field_name in section, (
                f"{name} waives '{dotted}' but '{field_name}' is not a "
                f"declared row of {section_name}"
            )


def test_active_clauses_waive_fields_before_their_deadline():
    waived = _load_grandfathered_fields(today=date(2026, 7, 1))
    assert "EntityProfile.vip_status" in waived
    assert "EntityProfile.credit_standing" in waived
    assert "ImpactMetrics.sla_deadline" in waived


def test_clauses_expire_automatically_after_their_deadline():
    # 2026-08-02 is past the two 2026-08-01 deadlines but before the
    # email_intake clause's 2026-08-04 (which waives no fields anyway).
    waived = _load_grandfathered_fields(today=date(2026, 8, 2))
    assert "EntityProfile.vip_status" not in waived
    assert "EntityProfile.credit_standing" not in waived
    assert "ImpactMetrics.sla_deadline" not in waived


def test_email_intake_clause_tracks_provenance_not_population():
    """floor_status is always populated by the StubGateway; waiving it
    would mask a broken gateway until the deadline. The clause exists
    for the stub-vs-real-connector provenance record only."""
    clause = _clauses()["email_intake_gateway_stub_only"]
    assert clause["fields"] == []


def test_structuring_clauses_changes_no_enforcement_today():
    """The waived fields are `tier: contextual` today (the registry
    rows say 'reclassify once a producer lands'). Contextual rows are
    skipped before the grandfather check, so restoring the structure
    must not alter current enforcement output."""
    from api.analysis_composer import _required_audit_fields
    from api.store import ChildCase

    record = ChildCase(
        tenant_id="t-test",
        order_id="ORD-001",
        event_type="GENERIC",
        trace_id="trace-1",
    )
    for class_name, field in [
        ("EntityProfile", "vip_status"),
        ("EntityProfile", "credit_standing"),
        ("ImpactMetrics", "sla_deadline"),
    ]:
        enforced, grandfathered = _required_audit_fields(record, class_name)
        assert field not in enforced
        assert field not in grandfathered
