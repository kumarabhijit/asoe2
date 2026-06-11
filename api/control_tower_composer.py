"""Control Tower composer — /dashboard redesign (sign-off 2026-06-10).

Assembles the `ControlTowerResponse` the operator dashboard projects,
entirely backend-side (asoe-ui Guardrail #6 — the UI does no
aggregation, no math). Pure functions over supplied records / case
summaries — tenant scoping, store access, and the RBAC dollar strip
are the ROUTE's job.

Honesty rules (Guardrail #6 / partial-truth veto):
  * No records → `auto_resolved_pct` is None, never a fabricated 0%.
  * Dollar sums emit only when every contributing impact shares one
    currency; mixed currencies → None (an honest sum is impossible).
  * Deltas ("vs last week") are NOT emitted — the store keeps no
    history baseline yet, so there is nothing honest to compare
    against. The UI omits the delta row.
  * Domain keys are raw taxonomy supergroup codes; the UI renders the
    governed label (Guardrail #1).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from api.schemas import (
    AgentDomainActivity,
    ControlTowerKpis,
    ControlTowerResponse,
    DollarAmount,
    IntentDollarMix,
    SlaRiskRow,
    ThroughputBucket,
)

# Records the agents finished without a human disposition.
_TERMINAL_STATES = frozenset({"RESOLVED", "CLOSED"})
# Records the agents are actively working (pre-handoff pipeline states).
_WORKING_STATES = frozenset({"INGESTED", "CLASSIFYING", "AUDITING"})

THROUGHPUT_WINDOW_HOURS = 8
SLA_RISK_WINDOW_HOURS = 8
SLA_RISK_ROW_CAP = 8


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sum_one_currency(
    amounts: Iterable[Optional[DollarAmount]],
) -> Optional[DollarAmount]:
    """Sum impacts when they all share one currency; None otherwise
    (mixed currencies cannot be summed honestly) or when nothing
    contributes."""
    total = 0
    currency: Optional[str] = None
    seen = False
    for a in amounts:
        if a is None:
            continue
        if currency is None:
            currency = a.currency
        elif a.currency != currency:
            return None
        total += a.amount_cents
        seen = True
    if not seen or currency is None:
        return None
    return DollarAmount(amount_cents=total, currency=currency)


def _impact_of(summary: Any) -> Optional[DollarAmount]:
    """CaseSummary.dollar_impact (dataclass) → DollarAmount, or None."""
    impact = getattr(summary, "dollar_impact", None)
    if impact is None:
        return None
    return DollarAmount(
        amount_cents=impact.amount_cents, currency=impact.currency
    )


def compose_control_tower(
    records: Sequence[Any],
    case_summaries: Sequence[Tuple[Any, Any]],
    *,
    now: Optional[datetime] = None,
) -> ControlTowerResponse:
    """Project records + (case, CaseSummary) pairs into the dashboard
    payload. `case_summaries` carry the attention disposition and the
    dollar impact the existing CaseSummary machinery already computes —
    this composer adds NO new business judgement, only roll-ups."""
    now = now or datetime.now(timezone.utc)

    needs_human = [
        (case, summary)
        for case, summary in case_summaries
        if getattr(summary, "attention_state", None) == "NEEDS_HUMAN"
    ]

    return ControlTowerResponse(
        kpis=_kpis(records, needs_human),
        throughput=_throughput(records, now),
        mix_by_intent=_mix_by_intent(needs_human),
        agent_activity=_agent_activity(records, now),
        sla_risk=_sla_risk(needs_human, now),
        generated_at=now.isoformat(),
    )


def _kpis(
    records: Sequence[Any],
    needs_human: Sequence[Tuple[Any, Any]],
) -> ControlTowerKpis:
    total = len(records)
    auto = 0
    resolution_seconds: List[float] = []
    for r in records:
        if r.lifecycle_state in _TERMINAL_STATES and not getattr(
            r, "resolved_by", None
        ):
            auto += 1
            created = _parse_iso(getattr(r, "created_at", None))
            updated = _parse_iso(getattr(r, "updated_at", None))
            if created and updated:
                resolution_seconds.append((updated - created).total_seconds())
    return ControlTowerKpis(
        auto_resolved_pct=(
            round(auto / total * 100.0, 1) if total > 0 else None
        ),
        open_needs_human=len(needs_human),
        avg_resolution_time_seconds=(
            round(sum(resolution_seconds) / len(resolution_seconds), 1)
            if resolution_seconds
            else None
        ),
        dollar_at_risk=_sum_one_currency(
            _impact_of(s) for _c, s in needs_human
        ),
    )


def _throughput(
    records: Sequence[Any], now: datetime
) -> List[ThroughputBucket]:
    """Resolutions per hour over the trailing window, split agents
    (no `resolved_by`) vs humans. Buckets emit even when zero so the
    chart's x-axis is stable."""
    window_start = (now - timedelta(hours=THROUGHPUT_WINDOW_HOURS - 1)).replace(
        minute=0, second=0, microsecond=0
    )
    buckets: Dict[datetime, ThroughputBucket] = {}
    for i in range(THROUGHPUT_WINDOW_HOURS):
        start = window_start + timedelta(hours=i)
        buckets[start] = ThroughputBucket(hour_start=start.isoformat())
    for r in records:
        if r.lifecycle_state not in _TERMINAL_STATES:
            continue
        finished = _parse_iso(getattr(r, "updated_at", None))
        if finished is None:
            continue
        hour = finished.replace(minute=0, second=0, microsecond=0)
        bucket = buckets.get(hour)
        if bucket is None:
            continue
        if getattr(r, "resolved_by", None):
            bucket.by_humans += 1
        else:
            bucket.by_agents += 1
    return [buckets[k] for k in sorted(buckets)]


def _mix_by_intent(
    needs_human: Sequence[Tuple[Any, Any]],
) -> List[IntentDollarMix]:
    """$ at risk per intent across NEEDS_HUMAN cases. A case without an
    intent or an impact contributes nothing (structural omission, not a
    zero row); per-intent mixed currencies drop that intent's row."""
    by_intent: Dict[str, List[DollarAmount]] = defaultdict(list)
    for _case, summary in needs_human:
        intent = getattr(summary, "intent", None)
        impact = _impact_of(summary)
        if not intent or impact is None:
            continue
        by_intent[intent].append(impact)
    rows: List[IntentDollarMix] = []
    for intent, impacts in by_intent.items():
        total = _sum_one_currency(impacts)
        if total is None:
            continue
        rows.append(IntentDollarMix(intent=intent, dollar_at_risk=total))
    rows.sort(
        key=lambda m: (-m.dollar_at_risk.amount_cents, m.intent),
    )
    return rows


def _agent_activity(
    records: Sequence[Any], now: datetime
) -> List[AgentDomainActivity]:
    """Per-taxonomy-domain runtime roll-up: records the agents are
    working right now + agent-resolved-today counts. Records without a
    supergroup classification are omitted (no fabricated domain)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    working: Dict[str, int] = defaultdict(int)
    resolved_today: Dict[str, int] = defaultdict(int)
    for r in records:
        domain = getattr(r, "supergroup_code", None)
        if not domain:
            continue
        if r.lifecycle_state in _WORKING_STATES:
            working[domain] += 1
        elif r.lifecycle_state in _TERMINAL_STATES and not getattr(
            r, "resolved_by", None
        ):
            finished = _parse_iso(getattr(r, "updated_at", None))
            if finished and finished >= midnight:
                resolved_today[domain] += 1
    domains = sorted(set(working) | set(resolved_today))
    return [
        AgentDomainActivity(
            domain=d,
            resolving_now=working.get(d, 0),
            resolved_today=resolved_today.get(d, 0),
        )
        for d in domains
    ]


def _sla_risk(
    needs_human: Sequence[Tuple[Any, Any]], now: datetime
) -> List[SlaRiskRow]:
    """NEEDS_HUMAN cases due within the window — including already
    breached (most urgent first). Cases without a parseable SLA are
    omitted: no deadline, no place in a deadline queue."""
    horizon = now + timedelta(hours=SLA_RISK_WINDOW_HOURS)
    rows: List[Tuple[datetime, SlaRiskRow]] = []
    for case, summary in needs_human:
        due = _parse_iso(getattr(case, "sla_due_at", None))
        if due is None or due > horizon:
            continue
        rows.append(
            (
                due,
                SlaRiskRow(
                    case_id=case.case_id,
                    customer_name=getattr(summary, "customer_name", None),
                    intent=getattr(summary, "intent", None),
                    sla_due_at=due.isoformat(),
                    dollar_impact=_impact_of(summary),
                ),
            )
        )
    rows.sort(key=lambda t: (t[0], t[1].case_id))
    return [row for _due, row in rows[:SLA_RISK_ROW_CAP]]
