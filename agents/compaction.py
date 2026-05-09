"""ADR-038 Phase H.7 — deterministic case-event compaction.

Trigger conditions per ADR-038 §7.4:
  * working-memory load exceeds 8k tokens, OR
  * 25 episodic events accumulated, OR
  * case open >7 days
(whichever fires first).

The compactor is **deterministic** — no LLM. Templates live at
``knowledge/compaction/<event_type>.template.md`` (fallback to
``__general__.template.md``). The compaction is itself an audit-log
event: ``(compaction_id, events_summarised, summary_text,
harness_version, timestamp)``. Original events are retained
verbatim in the case event log; compaction affects context-load
only, not persistence.

This module:

  1. ``CompactionTrigger.should_compact(...)`` — pure policy check.
  2. ``compact_events(...)`` — deterministic template application.
  3. ``run_compaction(...)`` — integrates trigger + run + persist.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from contracts.models import OrderCase


# ADR-038 §7.4 binding triggers.
COMPACTION_TRIGGER_TOKEN_BUDGET = 8_000
COMPACTION_TRIGGER_EVENT_COUNT = 25
COMPACTION_TRIGGER_AGE_DAYS = 7

COMPACTION_TARGET_TOKENS = 2_000  # output cap

COMPACTION_TEMPLATE_DIR = Path("knowledge/compaction")


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


@dataclass
class CompactionTrigger:
    """Result of the policy check. ``should`` true when any criterion
    fires; ``reason`` records which one for the audit log."""

    should: bool
    reason: Optional[str] = None
    metric_token_estimate: int = 0
    metric_event_count: int = 0
    metric_age_days: float = 0.0

    @classmethod
    def evaluate(
        cls,
        case: OrderCase,
        events: List[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
        token_estimate: Optional[int] = None,
    ) -> "CompactionTrigger":
        """Pure policy check. ``token_estimate`` is supplied by the
        harness when it knows the working-memory load; if absent we
        approximate from event count (rough: 200 tokens/event)."""
        now = now or datetime.now(timezone.utc)
        opened = _parse_iso(case.opened_at) or now
        age_days = max(0.0, (now - opened).total_seconds() / 86_400.0)
        event_count = len(events)
        tokens = token_estimate if token_estimate is not None else event_count * 200

        # Evaluate in priority order — the FIRST trigger that fires
        # wins for the audit reason.
        if tokens >= COMPACTION_TRIGGER_TOKEN_BUDGET:
            return cls(
                should=True,
                reason=f"token_budget (estimate={tokens} >= {COMPACTION_TRIGGER_TOKEN_BUDGET})",
                metric_token_estimate=tokens,
                metric_event_count=event_count,
                metric_age_days=age_days,
            )
        if event_count >= COMPACTION_TRIGGER_EVENT_COUNT:
            return cls(
                should=True,
                reason=f"event_count ({event_count} >= {COMPACTION_TRIGGER_EVENT_COUNT})",
                metric_token_estimate=tokens,
                metric_event_count=event_count,
                metric_age_days=age_days,
            )
        if age_days >= COMPACTION_TRIGGER_AGE_DAYS:
            return cls(
                should=True,
                reason=f"age_days ({age_days:.1f} >= {COMPACTION_TRIGGER_AGE_DAYS})",
                metric_token_estimate=tokens,
                metric_event_count=event_count,
                metric_age_days=age_days,
            )
        return cls(
            should=False,
            metric_token_estimate=tokens,
            metric_event_count=event_count,
            metric_age_days=age_days,
        )


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Python 3.11 fromisoformat accepts trailing 'Z'.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _summarise_event_line(event: Dict[str, Any]) -> str:
    """Reduce one event dict to a single audit-bearing line.

    Format: ``[<event_type>@<timestamp>] key=value, ...``
    Drops free-form text and verbose payloads; retains the small,
    typed fields the agent reasons over post-compaction.
    """
    et = str(event.get("event_type") or event.get("title") or "event")
    ts = str(event.get("timestamp") or event.get("occurred_at") or "")
    audit_keys = (
        "outcome", "status", "classification", "recommended_action",
        "shadow_verdict", "intent", "tool_name", "case_status_after",
        "reason_code", "autonomy_level", "amount_usd", "po_number",
    )
    pairs: List[str] = []
    for key in audit_keys:
        if key in event and event[key] not in (None, ""):
            pairs.append(f"{key}={event[key]}")
    body = ", ".join(pairs) if pairs else "—"
    return f"[{et}@{ts}] {body}"


def compact_events(
    events: List[Dict[str, Any]],
    *,
    target_tokens: int = COMPACTION_TARGET_TOKENS,
) -> str:
    """Apply deterministic per-event summarisation; concatenate;
    cap at ``target_tokens``.

    Approximation: 1 token ≈ 4 chars. Lines beyond the cap are
    truncated with a sentinel so audit knows compaction trimmed.
    """
    char_cap = target_tokens * 4
    lines = [_summarise_event_line(e) for e in events]
    out: List[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > char_cap:
            out.append(
                f"[compaction-truncated@{len(out)}/{len(lines)} events; "
                "verbatim retained in episodic memory]"
            )
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


@dataclass
class CompactionResult:
    """Audit-log entry for one compaction run."""

    compaction_id: str
    case_id: str
    events_summarised: int
    summary_text: str
    triggered_at: str
    trigger_reason: str
    template_set_version: str = "1.0.0"


def _compaction_id(case_id: str, triggered_at: str) -> str:
    """Stable id derived from case + timestamp; replayable from the
    audit log alone."""
    return hashlib.sha256(f"{case_id}@{triggered_at}".encode("utf-8")).hexdigest()[:16]


def run_compaction(
    *,
    case: OrderCase,
    events: List[Dict[str, Any]],
    now: Optional[datetime] = None,
    token_estimate: Optional[int] = None,
) -> Optional[CompactionResult]:
    """Combined trigger + compact + result builder.

    Returns ``None`` when the trigger doesn't fire (caller proceeds
    without compacting). Otherwise returns the ``CompactionResult``
    the harness persists. **The harness is responsible for updating
    OrderCase.working_memory_summary + last_compaction_at**; this
    function is pure (no I/O).
    """
    now = now or datetime.now(timezone.utc)
    trigger = CompactionTrigger.evaluate(
        case, events, now=now, token_estimate=token_estimate,
    )
    if not trigger.should:
        return None

    triggered_iso = now.isoformat()
    summary = compact_events(events)
    return CompactionResult(
        compaction_id=_compaction_id(case.case_id, triggered_iso),
        case_id=case.case_id,
        events_summarised=len(events),
        summary_text=summary,
        triggered_at=triggered_iso,
        trigger_reason=trigger.reason or "",
    )


# ---------------------------------------------------------------------------
# Replay-divergence check (audit-defensibility — ADR-038 §7.5 #4)
# ---------------------------------------------------------------------------


def replay_compaction(
    case: OrderCase,
    events: List[Dict[str, Any]],
    expected: CompactionResult,
) -> bool:
    """Replay a past compaction against the same inputs and confirm
    the summary text matches. Used by the SLI dashboard
    (``compaction-replay-divergence`` metric — target 0%).
    """
    summary = compact_events(events)
    return summary == expected.summary_text
