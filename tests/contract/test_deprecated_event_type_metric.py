"""ADR-034 §6.2 — deprecated event_type metric (S14).

Locks the SLI surface that the §28.6 Grafana dashboard scrapes
to track the legacy event_type cutover. Two invariants:

  1. `deprecated_event_type_received_total` increments on every
     inbound event whose `event_type` is in the §6.2 alias set,
     and is NOT incremented on the canonical name.
  2. `event_type_received_total` increments on every event,
     regardless of name. This surfaces canonical-vs-legacy
     side-by-side on the dashboard so producer teams see the
     migration without log queries.

When the §6.2 deadline (2026-08-12) passes, the deprecation
counter is retired in the same PR that flips hard rejection on
the legacy name. This test is removed at the same time;
`event_type_received_total` remains.
"""

from __future__ import annotations

import pytest

from api.metrics import (
    event_type_snapshot,
    record_event_type_received,
    render_event_type_metrics,
    reset_event_type_counters,
    _DEPRECATED_EVENT_TYPES,
)


@pytest.fixture(autouse=True)
def _reset_counters():
    reset_event_type_counters()
    yield
    reset_event_type_counters()


class TestEventTypeRecorder:
    def test_canonical_event_type_increments_total_only(self):
        record_event_type_received("MANUAL_ORDER_INTAKE")
        canonical, deprecated = event_type_snapshot()
        assert canonical == {"MANUAL_ORDER_INTAKE": 1}
        assert deprecated == {}

    def test_legacy_event_type_increments_both_counters(self):
        record_event_type_received("EMAIL_ORDER_ENTRY_REQUEST")
        canonical, deprecated = event_type_snapshot()
        assert canonical == {"EMAIL_ORDER_ENTRY_REQUEST": 1}
        assert deprecated == {"EMAIL_ORDER_ENTRY_REQUEST": 1}

    def test_short_form_legacy_alias_is_also_counted_as_deprecated(self):
        record_event_type_received("EMAIL_ORDER")
        canonical, deprecated = event_type_snapshot()
        assert canonical == {"EMAIL_ORDER": 1}
        assert deprecated == {"EMAIL_ORDER": 1}

    def test_multiple_observations_accumulate(self):
        for _ in range(3):
            record_event_type_received("MANUAL_ORDER_INTAKE")
        for _ in range(2):
            record_event_type_received("EMAIL_ORDER_ENTRY_REQUEST")
        canonical, deprecated = event_type_snapshot()
        assert canonical == {
            "MANUAL_ORDER_INTAKE": 3,
            "EMAIL_ORDER_ENTRY_REQUEST": 2,
        }
        assert deprecated == {"EMAIL_ORDER_ENTRY_REQUEST": 2}


class TestDeprecatedEventTypesContents:
    """The §6.2 alias set is the source of truth — every legacy
    name the codebase still accepts MUST be listed here so the
    deprecation counter catches it. Adding a new alias without
    updating this set is a CLAUDE.md §3 (audit-bearing) violation."""

    def test_legacy_request_name_is_listed(self):
        assert "EMAIL_ORDER_ENTRY_REQUEST" in _DEPRECATED_EVENT_TYPES

    def test_short_form_legacy_name_is_listed(self):
        assert "EMAIL_ORDER" in _DEPRECATED_EVENT_TYPES

    def test_canonical_name_is_NOT_in_deprecation_set(self):
        # A regression check — if `MANUAL_ORDER_INTAKE` slipped
        # into the deprecation set, every healthy inbound event
        # would be counted as deprecated and the deadline
        # dashboard would show a flat line at 100%.
        assert "MANUAL_ORDER_INTAKE" not in _DEPRECATED_EVENT_TYPES

    def test_each_deprecated_entry_points_at_an_adr_section(self):
        # Every legacy name must carry an ADR-pointer label so the
        # dashboard groups by deadline cohort.
        for et, label in _DEPRECATED_EVENT_TYPES.items():
            assert label, f"{et} has no ADR-pointer label"
            assert "adr-" in label.lower(), (
                f"{et} label {label!r} must reference an ADR section"
            )


class TestPrometheusRendering:
    def test_empty_state_renders_nothing(self):
        # Avoids zero-cardinality lines on a fresh process — the
        # dashboard expects metric absence, not "0".
        assert render_event_type_metrics() == ""

    def test_canonical_only_emits_received_total_but_no_deprecated_block(self):
        record_event_type_received("MANUAL_ORDER_INTAKE")
        text = render_event_type_metrics()
        assert "event_type_received_total" in text
        assert 'event_type="MANUAL_ORDER_INTAKE"' in text
        # Deprecation block is absent until a legacy name is observed.
        assert "deprecated_event_type_received_total" not in text

    def test_legacy_observation_emits_both_blocks(self):
        record_event_type_received("EMAIL_ORDER_ENTRY_REQUEST")
        text = render_event_type_metrics()
        assert "event_type_received_total" in text
        assert "deprecated_event_type_received_total" in text
        # The ADR-pointer label rides with the deprecation line.
        assert 'deprecated_for="adr-034-§6.2"' in text or 'deprecated_for=' in text

    def test_help_and_type_lines_present(self):
        record_event_type_received("EMAIL_ORDER_ENTRY_REQUEST")
        text = render_event_type_metrics()
        # Prometheus parser requires HELP+TYPE for each metric family.
        assert "# HELP event_type_received_total" in text
        assert "# TYPE event_type_received_total counter" in text
        assert "# HELP deprecated_event_type_received_total" in text
        assert "# TYPE deprecated_event_type_received_total counter" in text


class TestRenderAllIntegration:
    """`render_all()` is the single entrypoint the metrics route
    calls. Verify the new event-type block lands there too."""

    def test_render_all_includes_event_type_metrics_when_present(self):
        from api.metrics import render_all
        record_event_type_received("MANUAL_ORDER_INTAKE")
        text = render_all()
        assert "event_type_received_total" in text
