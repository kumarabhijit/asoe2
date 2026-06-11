"""Control Tower composer locks (sign-off 2026-06-10).

Pure-function tests over fixture records / (case, summary) pairs — no
store, no network. Lock the honesty rules: None over fabricated zeros,
single-currency sums only, attention-driven membership, stable
ordering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from api.case_summary import DollarImpact
from api.control_tower_composer import (
    SLA_RISK_ROW_CAP,
    THROUGHPUT_WINDOW_HOURS,
    compose_control_tower,
)

NOW = datetime(2026, 6, 10, 15, 30, tzinfo=timezone.utc)


def _record(
    id: str,
    *,
    lifecycle_state: str = "RESOLVED",
    resolved_by: str | None = None,
    supergroup_code: str | None = "SG_BLOCK_PRICING",
    created_at: str = "2026-06-10T14:00:00+00:00",
    updated_at: str = "2026-06-10T15:00:00+00:00",
):
    return SimpleNamespace(
        id=id,
        lifecycle_state=lifecycle_state,
        resolved_by=resolved_by,
        supergroup_code=supergroup_code,
        created_at=created_at,
        updated_at=updated_at,
    )


def _case_pair(
    case_id: str,
    *,
    attention: str = "NEEDS_HUMAN",
    intent: str | None = "PRICE_HOLD_RELEASE",
    customer: str | None = "Kroger",
    impact_cents: int | None = 1210000,
    currency: str = "USD",
    sla_due_at: str | None = "2026-06-10T16:33:00+00:00",
):
    case = SimpleNamespace(case_id=case_id, sla_due_at=sla_due_at)
    summary = SimpleNamespace(
        attention_state=attention,
        intent=intent,
        customer_name=customer,
        dollar_impact=(
            DollarImpact(amount_cents=impact_cents, currency=currency)
            if impact_cents is not None
            else None
        ),
    )
    return (case, summary)


# ── KPIs ───────────────────────────────────────────────────────────────


def test_kpis_empty_tenant_emits_none_not_zero_pct():
    out = compose_control_tower([], [], now=NOW)
    assert out.kpis.auto_resolved_pct is None
    assert out.kpis.avg_resolution_time_seconds is None
    assert out.kpis.dollar_at_risk is None
    assert out.kpis.open_needs_human == 0


def test_kpis_auto_resolved_counts_agent_finishes_only():
    records = [
        _record("a"),                              # agent-resolved
        _record("b", resolved_by="manager@acme"),  # human disposition
        _record("c", lifecycle_state="PENDING_REVIEW"),
        _record("d", lifecycle_state="CLOSED"),    # agent-closed
    ]
    out = compose_control_tower(records, [], now=NOW)
    assert out.kpis.auto_resolved_pct == 50.0  # 2 of 4


def test_kpis_dollar_at_risk_sums_needs_human_single_currency():
    pairs = [
        _case_pair("c1", impact_cents=1000),
        _case_pair("c2", impact_cents=500),
        _case_pair("c3", attention="DONE", impact_cents=99999),  # excluded
        _case_pair("c4", impact_cents=None),  # contributes nothing
    ]
    out = compose_control_tower([], pairs, now=NOW)
    assert out.kpis.open_needs_human == 3
    assert out.kpis.dollar_at_risk is not None
    assert out.kpis.dollar_at_risk.amount_cents == 1500
    assert out.kpis.dollar_at_risk.currency == "USD"


def test_kpis_mixed_currencies_emit_none_not_a_dishonest_sum():
    pairs = [
        _case_pair("c1", impact_cents=1000, currency="USD"),
        _case_pair("c2", impact_cents=500, currency="EUR"),
    ]
    out = compose_control_tower([], pairs, now=NOW)
    assert out.kpis.dollar_at_risk is None


# ── throughput ─────────────────────────────────────────────────────────


def test_throughput_emits_stable_window_and_splits_agents_humans():
    records = [
        _record("a", updated_at="2026-06-10T15:10:00+00:00"),
        _record("b", updated_at="2026-06-10T15:20:00+00:00", resolved_by="x"),
        _record("c", updated_at="2026-06-10T09:10:00+00:00"),
        _record("old", updated_at="2026-06-09T15:10:00+00:00"),  # outside
        _record("open", lifecycle_state="PENDING_REVIEW"),       # not terminal
    ]
    out = compose_control_tower(records, [], now=NOW)
    assert len(out.throughput) == THROUGHPUT_WINDOW_HOURS
    # Buckets are chronological and zero-filled.
    hours = [b.hour_start for b in out.throughput]
    assert hours == sorted(hours)
    last = out.throughput[-1]
    assert last.hour_start == "2026-06-10T15:00:00+00:00"
    assert last.by_agents == 1 and last.by_humans == 1
    first = out.throughput[0]
    assert first.by_agents + first.by_humans == 0


# ── mix by intent ──────────────────────────────────────────────────────


def test_mix_groups_needs_human_dollars_by_intent_sorted_desc():
    pairs = [
        _case_pair("c1", intent="PRICE_HOLD_RELEASE", impact_cents=1000),
        _case_pair("c2", intent="PRICE_HOLD_RELEASE", impact_cents=2000),
        _case_pair("c3", intent="CREDIT_BLOCK", impact_cents=9000),
        _case_pair("c4", intent=None, impact_cents=500),       # no intent → omitted
        _case_pair("c5", intent="DUPLICATE_PO", impact_cents=None),  # no $ → omitted
        _case_pair("c6", attention="DONE", intent="CREDIT_BLOCK", impact_cents=7777),
    ]
    out = compose_control_tower([], pairs, now=NOW)
    assert [(m.intent, m.dollar_at_risk.amount_cents) for m in out.mix_by_intent] == [
        ("CREDIT_BLOCK", 9000),
        ("PRICE_HOLD_RELEASE", 3000),
    ]


# ── agent activity ─────────────────────────────────────────────────────


def test_agent_activity_rolls_up_by_domain():
    records = [
        _record("w1", lifecycle_state="CLASSIFYING"),
        _record("w2", lifecycle_state="AUDITING"),
        _record("r1"),  # agent-resolved today (updated 15:00 / NOW 15:30)
        _record("r2", resolved_by="human"),  # human-resolved → not agent count
        _record(
            "y1", updated_at="2026-06-09T10:00:00+00:00"
        ),  # resolved yesterday
        _record("n1", supergroup_code=None),  # unclassified → omitted
    ]
    out = compose_control_tower(records, [], now=NOW)
    assert len(out.agent_activity) == 1
    row = out.agent_activity[0]
    assert row.domain == "SG_BLOCK_PRICING"
    assert row.resolving_now == 2
    assert row.resolved_today == 1


# ── SLA risk ───────────────────────────────────────────────────────────


def test_sla_risk_window_includes_breached_sorts_by_deadline_and_caps():
    pairs = [
        _case_pair("due-soon", sla_due_at="2026-06-10T16:33:00+00:00"),
        _case_pair("breached", sla_due_at="2026-06-10T14:00:00+00:00"),
        _case_pair("far", sla_due_at="2026-06-11T09:00:00+00:00"),  # > 8h
        _case_pair("no-sla", sla_due_at=None),
        _case_pair("done", attention="DONE", sla_due_at="2026-06-10T16:00:00+00:00"),
    ]
    out = compose_control_tower([], pairs, now=NOW)
    assert [r.case_id for r in out.sla_risk] == ["breached", "due-soon"]
    row = out.sla_risk[0]
    assert row.customer_name == "Kroger"
    assert row.dollar_impact is not None
    # Timestamp, never a pre-rendered label (UI ticker owns 'due in').
    assert row.sla_due_at.startswith("2026-06-10T14:00")


def test_sla_risk_caps_rows():
    pairs = [
        _case_pair(
            f"c{i}", sla_due_at=f"2026-06-10T16:{i:02d}:00+00:00"
        )
        for i in range(SLA_RISK_ROW_CAP + 4)
    ]
    out = compose_control_tower([], pairs, now=NOW)
    assert len(out.sla_risk) == SLA_RISK_ROW_CAP
