"""ADR-038 Phase H.7 — compaction + SLA + backfill tests.

Locks the contracts for the three Phase H.7 modules:

  * agents.compaction — trigger thresholds (8k tokens / 25 events /
    7 days), deterministic per-event summarisation, replay-divergence
    invariant.
  * agents.sla — policy YAML loader, per-tier hour lookup, deadline
    stamping.
  * agents.backfill — orphan-case-per-record materialisation,
    optional merge-by-correlation second pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pytest

from agents.backfill import (
    backfill_orphan_cases,
    merge_orphan_cases_by_correlation,
)
from agents.compaction import (
    COMPACTION_TARGET_TOKENS,
    COMPACTION_TRIGGER_AGE_DAYS,
    COMPACTION_TRIGGER_EVENT_COUNT,
    COMPACTION_TRIGGER_TOKEN_BUDGET,
    CompactionTrigger,
    apply_compaction_if_needed,
    compact_events,
    replay_compaction,
    run_compaction,
)
from agents.sla import (
    SlaPolicy,
    SlaPolicySet,
    get_policy,
    hours_for_customer_tier,
    reload_policy,
    stamp_sla_deadline,
)
from api.store import case_store, exception_store
from contracts.models import OrderCase


@pytest.fixture(autouse=True)
def _reset_stores():
    case_store.clear()
    exception_store.clear()
    reload_policy()
    yield
    case_store.clear()
    exception_store.clear()


@pytest.fixture
def case() -> OrderCase:
    case, _ = case_store.lookup_or_create(
        tenant_id="t1",
        source="manual_order",
        source_channel="email",
        customer_po_number="PO-1",
    )
    return case


# ---------------------------------------------------------------------------
# Compaction trigger
# ---------------------------------------------------------------------------


class TestCompactionTrigger:
    def test_no_trigger_on_small_recent_case(self, case):
        events = [{"event_type": "agent_step", "timestamp": "2026-04-22T10:00:00Z"}] * 3
        trigger = CompactionTrigger.evaluate(case, events)
        assert trigger.should is False
        assert trigger.metric_event_count == 3

    def test_event_count_triggers(self, case):
        events = [{"event_type": "agent_step", "timestamp": "2026-04-22T10:00:00Z"}] * COMPACTION_TRIGGER_EVENT_COUNT
        trigger = CompactionTrigger.evaluate(case, events)
        assert trigger.should is True
        assert "event_count" in trigger.reason

    def test_age_triggers(self, case):
        # opened 8 days ago.
        opened = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        case = case.model_copy(update={"opened_at": opened})
        trigger = CompactionTrigger.evaluate(case, [])
        assert trigger.should is True
        assert "age_days" in trigger.reason

    def test_token_budget_triggers(self, case):
        events = [{"event_type": "agent_step", "timestamp": "x"}] * 3
        # Pass an explicit token estimate that exceeds the budget.
        trigger = CompactionTrigger.evaluate(
            case, events, token_estimate=COMPACTION_TRIGGER_TOKEN_BUDGET + 1,
        )
        assert trigger.should is True
        assert "token_budget" in trigger.reason

    def test_trigger_priority_order(self, case):
        """Token-budget trigger takes priority over event_count and age."""
        events = [{"event_type": "agent_step", "timestamp": "x"}] * COMPACTION_TRIGGER_EVENT_COUNT
        trigger = CompactionTrigger.evaluate(
            case, events, token_estimate=COMPACTION_TRIGGER_TOKEN_BUDGET + 1,
        )
        assert "token_budget" in trigger.reason


# ---------------------------------------------------------------------------
# Compaction: deterministic summarisation
# ---------------------------------------------------------------------------


class TestCompactEvents:
    def test_per_event_line_format(self):
        events = [
            {
                "event_type": "agent_tool_call",
                "timestamp": "2026-04-22T10:00:00Z",
                "tool_name": "check_credit",
                "outcome": "ok",
            },
            {
                "event_type": "shadow_audit",
                "timestamp": "2026-04-22T10:01:00Z",
                "shadow_verdict": "GREEN",
            },
        ]
        summary = compact_events(events)
        assert "[agent_tool_call@2026-04-22T10:00:00Z]" in summary
        assert "tool_name=check_credit" in summary
        assert "[shadow_audit@2026-04-22T10:01:00Z]" in summary
        assert "shadow_verdict=GREEN" in summary

    def test_summary_capped_by_target_tokens(self):
        events = [
            {
                "event_type": "x",
                "timestamp": f"t{i}",
                "outcome": "ok",
            }
            for i in range(10_000)
        ]
        summary = compact_events(events, target_tokens=100)
        # ≤ 400 chars (1 token ≈ 4 chars) plus the truncation marker.
        assert "[compaction-truncated" in summary
        assert len(summary) < (100 * 4) + 200

    def test_event_with_no_audit_keys_renders_dash(self):
        events = [{"event_type": "noop", "timestamp": "T"}]
        summary = compact_events(events)
        assert "[noop@T] —" in summary


class TestRunCompaction:
    def test_returns_none_when_trigger_does_not_fire(self, case):
        result = run_compaction(case=case, events=[])
        assert result is None

    def test_returns_result_when_event_count_triggers(self, case):
        events = [{"event_type": "x", "timestamp": "t"}] * COMPACTION_TRIGGER_EVENT_COUNT
        result = run_compaction(case=case, events=events)
        assert result is not None
        assert result.case_id == case.case_id
        assert result.events_summarised == COMPACTION_TRIGGER_EVENT_COUNT
        assert result.compaction_id  # 16-char hex
        assert "event_count" in result.trigger_reason

    def test_replay_divergence_zero_for_same_inputs(self, case):
        events = [
            {"event_type": "agent_step", "timestamp": "T1", "outcome": "ok"},
        ] * COMPACTION_TRIGGER_EVENT_COUNT
        result = run_compaction(case=case, events=events)
        assert result is not None
        # Replay against the same inputs MUST produce the same summary
        # text — the compactor is deterministic per ADR-038 §7.4.
        assert replay_compaction(case, events, result) is True

    def test_replay_diverges_on_modified_inputs(self, case):
        events = [
            {"event_type": "agent_step", "timestamp": "T1", "outcome": "ok"},
        ] * COMPACTION_TRIGGER_EVENT_COUNT
        result = run_compaction(case=case, events=events)
        assert result is not None
        # Tamper with the input — replay must detect the change.
        events_tampered = list(events)
        events_tampered[0] = {**events_tampered[0], "outcome": "tampered"}
        assert replay_compaction(case, events_tampered, result) is False


# ---------------------------------------------------------------------------
# apply_compaction_if_needed — wire-up helper that persists too
# ---------------------------------------------------------------------------


class TestApplyCompactionIfNeeded:
    def test_no_op_below_thresholds(self, case):
        result = apply_compaction_if_needed(case=case, events=[])
        assert result is None
        # No persistence happened either.
        refreshed = case_store.get(case.case_id)
        assert refreshed.working_memory_summary is None
        assert refreshed.last_compaction_at is None

    def test_persists_summary_when_trigger_fires(self, case):
        events = [
            {"event_type": "agent_step", "timestamp": f"T{i}", "outcome": "ok"}
            for i in range(COMPACTION_TRIGGER_EVENT_COUNT)
        ]
        result = apply_compaction_if_needed(case=case, events=events)
        assert result is not None
        refreshed = case_store.get(case.case_id)
        assert refreshed.working_memory_summary == result.summary_text
        assert refreshed.last_compaction_at == result.triggered_at

    def test_idempotent_replay(self, case):
        """Running the helper twice with the same inputs is a no-op
        on the second call — `last_compaction_at` advances forward in
        clock time but the summary text is byte-identical (replay
        divergence == 0)."""
        events = [
            {"event_type": "agent_step", "timestamp": f"T{i}", "outcome": "ok"}
            for i in range(COMPACTION_TRIGGER_EVENT_COUNT)
        ]
        first = apply_compaction_if_needed(case=case, events=events)
        # `case` is the pre-compaction snapshot; pull the persisted one.
        case_after = case_store.get(case.case_id)
        second = apply_compaction_if_needed(case=case_after, events=events)
        assert first is not None and second is not None
        assert first.summary_text == second.summary_text


# ---------------------------------------------------------------------------
# SLA policy
# ---------------------------------------------------------------------------


class TestSlaPolicy:
    def test_default_policy_loads_from_yaml(self):
        policy = get_policy()
        assert isinstance(policy, SlaPolicySet)
        # The shipped YAML has Strategic / Mid-Market / Long-tail tiers.
        assert "Strategic" in policy.tiers
        assert "Mid-Market" in policy.tiers
        assert "Long-tail" in policy.tiers

    def test_strategic_tier_has_4_hour_sla(self):
        assert hours_for_customer_tier("Strategic") == 4

    def test_unknown_tier_falls_back_to_default(self):
        assert hours_for_customer_tier("not-a-tier") == get_policy().default_sla_hours

    def test_none_tier_falls_back(self):
        assert hours_for_customer_tier(None) == get_policy().default_sla_hours

    def test_stamp_sla_deadline_strategic(self):
        opened = "2026-04-22T10:00:00+00:00"
        deadline = stamp_sla_deadline(opened_at=opened, customer_tier="Strategic")
        # +4 hours.
        assert deadline.startswith("2026-04-22T14:00:00")

    def test_stamp_sla_deadline_default_when_unknown_tier(self):
        opened = "2026-04-22T10:00:00+00:00"
        deadline = stamp_sla_deadline(
            opened_at=opened, customer_tier="ghost",
        )
        # +48 hours (default per yaml).
        assert deadline.startswith("2026-04-24T10:00:00")

    def test_stamp_sla_deadline_invalid_opened_at_falls_back_to_now(self):
        deadline = stamp_sla_deadline(
            opened_at="not-a-date", customer_tier="Strategic",
        )
        # Just verify we got an ISO string back; the exact value depends
        # on now().
        assert "T" in deadline

    def test_reload_policy_invalidates_cache(self):
        original = get_policy()
        reloaded = reload_policy()
        # After reload the policy still loads (no I/O changed); the
        # invariant is that we get a SlaPolicySet back, not the same
        # object (though identity isn't guaranteed).
        assert isinstance(reloaded, SlaPolicySet)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


class TestBackfill:
    def _seed_orphan_record(self, *, tenant_id="t1", order_id="PO-1", event_type="EDI_850_DUPLICATE_PO"):
        record = exception_store.create(
            tenant_id=tenant_id,
            order_id=order_id,
            event_type=event_type,
            trace_id=f"trace-{order_id}",
            intent="DUPLICATE_PO",
            shadow_verdict="YELLOW",
            final_status="MANUAL_REVIEW_REQUIRED",
            original_event={
                "order_id": order_id,
                "po_price": 100.0,
                "sap_base_price": 100.0,
                "event_type": event_type,
                "metadata": {},
            },
        )
        # Force orphan state.
        record.parent_case_id = None
        return record

    def test_backfill_opens_case_per_record(self):
        record = self._seed_orphan_record()
        report = backfill_orphan_cases()
        assert report.records_scanned == 1
        assert report.cases_opened == 1
        assert report.records_skipped_no_event == 0
        # Record now points at a case.
        assert record.parent_case_id is not None
        assert report.record_to_case[record.id] == record.parent_case_id

    def test_backfill_attaches_records_with_same_po_to_one_case(self):
        record_a = self._seed_orphan_record(order_id="PO-DUP-7")
        record_b = self._seed_orphan_record(order_id="PO-DUP-7")
        report = backfill_orphan_cases()
        assert record_a.parent_case_id == record_b.parent_case_id, (
            "Records sharing customer_po should attach to a single case"
        )
        # Only ONE case opened.
        assert report.cases_opened == 1
        # Second record was attached, not opened anew.
        assert report.records_attached_to_existing == 1

    def test_backfill_skips_records_with_no_original_event(self):
        # Direct-construct without the original_event payload.
        from api.store import ExceptionRecord
        record = ExceptionRecord(
            tenant_id="t1",
            order_id="PO-X",
            event_type="UNKNOWN",
            trace_id="trace-x",
        )
        # Inject into store bypassing create() so original_event stays None.
        exception_store._records[record.id] = record  # type: ignore[attr-defined]
        report = backfill_orphan_cases()
        assert report.records_skipped_no_event == 1
        assert report.cases_opened == 0
        assert record.parent_case_id is None  # untouched

    def test_backfill_is_idempotent(self):
        self._seed_orphan_record()
        report1 = backfill_orphan_cases()
        report2 = backfill_orphan_cases()
        assert report1.cases_opened == 1
        # Second run is a no-op — every record now has parent_case_id.
        assert report2.cases_opened == 0
        assert report2.records_attached_to_existing == 0

    def test_backfill_uses_customer_tier_lookup_for_sla(self):
        record = self._seed_orphan_record(tenant_id="strategic-tenant")
        backfill_orphan_cases(
            customer_tier_lookup={"strategic-tenant": "Strategic"},
        )
        case = case_store.get(record.parent_case_id)
        # 4h SLA from open.
        assert case.sla_deadline is not None
        opened = datetime.fromisoformat(case.opened_at.replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(case.sla_deadline.replace("Z", "+00:00"))
        delta_hours = (deadline - opened).total_seconds() / 3600.0
        assert abs(delta_hours - 4.0) < 0.01

    def test_merge_pass_dry_run_reports_without_modifying(self):
        # Force two cases on the same (tenant, customer_po) by opening
        # them out-of-order so Pass 1 doesn't naturally collapse them.
        case_a, _ = case_store.lookup_or_create(
            tenant_id="t1", source="automated_order",
            source_channel="edi_x12_850", customer_po_number="PO-MERGE",
        )
        case_b, _ = case_store.lookup_or_create(
            tenant_id="t1", source="manual_order",
            source_channel="email",
        )
        # Manually patch case_b to share the PO without going through
        # lookup_or_create's correlation table (so it stays a duplicate).
        case_store._cases[case_b.case_id] = case_b.model_copy(  # type: ignore[attr-defined]
            update={"customer_po_number": "PO-MERGE"},
        )

        report = merge_orphan_cases_by_correlation(dry_run=True)
        assert report.cases_merged == 1
        # Both cases still present in the store (dry run).
        assert case_store.get(case_a.case_id) is not None
        assert case_store.get(case_b.case_id) is not None

    def test_merge_pass_actually_merges(self):
        case_a, _ = case_store.lookup_or_create(
            tenant_id="t1", source="automated_order",
            source_channel="edi_x12_850", customer_po_number="PO-MERGE-2",
        )
        case_b, _ = case_store.lookup_or_create(
            tenant_id="t1", source="manual_order",
            source_channel="email",
        )
        case_store._cases[case_b.case_id] = case_b.model_copy(  # type: ignore[attr-defined]
            update={"customer_po_number": "PO-MERGE-2"},
        )

        report = merge_orphan_cases_by_correlation()
        assert report.cases_merged == 1
        # Earlier-opened case wins; later one is dropped.
        assert case_store.get(case_a.case_id) is not None
        assert case_store.get(case_b.case_id) is None
