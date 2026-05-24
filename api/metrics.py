"""Prometheus-formatted metrics emitter.

ADR-039 §7.3 SLI surface — emits the
``compliance.shadow_llm.ShadowLLMMetrics`` counters in
Prometheus text exposition format. Read by Grafana via the
existing scrape config.

We deliberately do **not** depend on the `prometheus_client`
package — the text format is a small, stable protocol and the
only thing we publish today is a single namespace. Adding the
client would also add the default-collector noise (process /
gc / fd metrics) which our scrape config does not consume.

Emitted metric families (per ADR-039 §7.3):

  shadow_llm_invocations_total          counter
  shadow_llm_invocations_by_trigger     counter (label: trigger)
  shadow_llm_cache_hits_total           counter
  shadow_llm_verdicts_total             counter (label: action)
  shadow_llm_timeouts_total             counter
  shadow_llm_unavailable_total          counter
  shadow_llm_validation_errors_total    counter
  shadow_llm_skipped_red_total          counter
  shadow_llm_skipped_below_floor_total  counter
  shadow_llm_latency_ms_sum             gauge
  shadow_llm_latency_ms_count           gauge
  shadow_llm_cost_usd_total             gauge

Derived ratios are emitted as gauges for dashboard convenience:

  shadow_llm_disagreement_rate          gauge   (DISAGREE_DOWNGRADE / total)
  shadow_llm_abstain_rate               gauge   (ABSTAIN / total)
  shadow_llm_cache_hit_rate             gauge   (cache_hits / total)
  shadow_llm_avg_latency_ms             gauge   (sum / count)

Reviewer-override surface (ADR-039 §6.3 X.2→X.3 gate):

  shadow_llm_reviewer_overrides_of_downgrade_total  counter
  shadow_llm_reviewer_override_rate_on_downgrades   gauge   (counter / DISAGREE_DOWNGRADE verdicts)
"""

from __future__ import annotations

from typing import Iterable

from compliance.shadow_llm import ShadowLLMMetrics, shadow_llm_metrics


# ---------------------------------------------------------------------------
# Per-request case-list SLI (Phase 28.5.x §D7 — pagination deferral)
# ---------------------------------------------------------------------------
#
# `cases_returned_per_request` tracks how many cases a /api/v1/cases
# response carried. The §D7 decision deferred cursor pagination, with
# a re-open trigger when any tenant approaches 150 open cases. This
# SLI is the trigger: when the p99 of this gauge sustains ≥ 150, the
# dashboard surfaces a "shift to server-side pagination" alert.
#
# Tracked as a rolling window (last N requests) rather than a
# histogram — the values are small integers (0..500 limit cap) and
# we only need p95 / p99 for the alert; a simple list-as-circular-
# buffer keeps the calculation overhead negligible.

from collections import deque
from threading import Lock as _CaseLock
from typing import Deque

_CASE_REQUEST_WINDOW = 1024  # last 1024 /api/v1/cases responses
_case_request_lock = _CaseLock()
_case_request_window: Deque[int] = deque(maxlen=_CASE_REQUEST_WINDOW)


def record_cases_returned(count: int) -> None:
    """Record one /api/v1/cases response's payload size. Called by
    the route handler immediately before serialising the response.
    Tenant-agnostic intentionally: the pagination decision is global,
    not per-tenant (a single big-tenant trigger forces the rollout
    for everyone).
    """
    with _case_request_lock:
        _case_request_window.append(count)


def cases_returned_snapshot() -> tuple[int, float, int, int]:
    """Return `(samples, avg, p95, p99)` over the rolling window.
    Returns `(0, 0.0, 0, 0)` when the window is empty so the gauge
    plots as zero (Prometheus 0-baseline contract from §28.6)."""
    with _case_request_lock:
        if not _case_request_window:
            return 0, 0.0, 0, 0
        sorted_window = sorted(_case_request_window)
        n = len(sorted_window)
        avg = sum(sorted_window) / n
        p95_idx = max(0, min(n - 1, int(0.95 * (n - 1))))
        p99_idx = max(0, min(n - 1, int(0.99 * (n - 1))))
        return n, avg, sorted_window[p95_idx], sorted_window[p99_idx]


def reset_cases_returned_window() -> None:
    """Test helper — drop all samples."""
    with _case_request_lock:
        _case_request_window.clear()


def _line(name: str, value: float, *, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    label_str = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return f"{name}{{{label_str}}} {value}"


def _escape(s: str) -> str:
    """Prometheus label-value escape rules (backslash, double-quote,
    newline)."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _help_type(name: str, help_text: str, metric_type: str) -> Iterable[str]:
    yield f"# HELP {name} {help_text}"
    yield f"# TYPE {name} {metric_type}"


def render_shadow_llm_metrics(metrics: ShadowLLMMetrics) -> str:
    """Render a ShadowLLMMetrics snapshot as Prometheus text.

    Pure function — accepts the snapshot, returns the body. The
    HTTP route handler reads `shadow_llm_metrics` once and passes
    it here so we don't observe a counter mid-mutation.
    """
    lines: list[str] = []

    # Invocation counters
    lines.extend(_help_type(
        "shadow_llm_invocations_total",
        "Total L2 LLM Shadow invocations (cache hits + cold-path).",
        "counter",
    ))
    lines.append(_line(
        "shadow_llm_invocations_total", metrics.invocations_total,
    ))

    lines.extend(_help_type(
        "shadow_llm_invocations_by_trigger",
        "L2 invocations broken down by gating trigger.",
        "counter",
    ))
    for trigger, count in sorted(metrics.invocations_by_trigger.items()):
        lines.append(_line(
            "shadow_llm_invocations_by_trigger", count,
            labels={"trigger": trigger},
        ))

    lines.extend(_help_type(
        "shadow_llm_cache_hits_total",
        "Verdicts served from the L4 per-tenant cache (ADR-039 §5.5).",
        "counter",
    ))
    lines.append(_line("shadow_llm_cache_hits_total", metrics.cache_hits_total))

    # Verdict counters
    lines.extend(_help_type(
        "shadow_llm_verdicts_total",
        "L2 verdicts emitted, by action.",
        "counter",
    ))
    for action in ("AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"):
        lines.append(_line(
            "shadow_llm_verdicts_total",
            metrics.verdicts_by_action.get(action, 0),
            labels={"action": action},
        ))

    # Failure counters
    lines.extend(_help_type(
        "shadow_llm_timeouts_total",
        "Provider exceeded the wall-clock timeout (ADR-039 §5.3).",
        "counter",
    ))
    lines.append(_line("shadow_llm_timeouts_total", metrics.timeouts_total))

    lines.extend(_help_type(
        "shadow_llm_unavailable_total",
        "Provider returned 5xx / connection error.",
        "counter",
    ))
    lines.append(_line("shadow_llm_unavailable_total", metrics.unavailable_total))

    lines.extend(_help_type(
        "shadow_llm_validation_errors_total",
        "Constrained-generation defects (out-of-vocab concerns / "
        "schema mismatch / 400 from provider).",
        "counter",
    ))
    lines.append(_line(
        "shadow_llm_validation_errors_total", metrics.validation_errors_total,
    ))

    # Skip counters
    lines.extend(_help_type(
        "shadow_llm_skipped_red_total",
        "Deterministic-RED short-circuited L2 (never invoked).",
        "counter",
    ))
    lines.append(_line(
        "shadow_llm_skipped_red_total", metrics.skipped_red_total,
    ))

    lines.extend(_help_type(
        "shadow_llm_skipped_below_floor_total",
        "GREEN events below the financial-impact floor — L2 not invoked.",
        "counter",
    ))
    lines.append(_line(
        "shadow_llm_skipped_below_floor_total", metrics.skipped_below_floor_total,
    ))

    # Latency
    lines.extend(_help_type(
        "shadow_llm_latency_ms_sum",
        "Cumulative wall-clock latency in milliseconds (cold-path only).",
        "gauge",
    ))
    lines.append(_line("shadow_llm_latency_ms_sum", metrics.latency_ms_sum))

    lines.extend(_help_type(
        "shadow_llm_latency_ms_count",
        "Cold-path invocation count (paired with latency_ms_sum).",
        "gauge",
    ))
    lines.append(_line("shadow_llm_latency_ms_count", metrics.latency_ms_count))

    # Cost
    lines.extend(_help_type(
        "shadow_llm_cost_usd_total",
        "Cumulative provider cost estimate in USD.",
        "gauge",
    ))
    lines.append(_line("shadow_llm_cost_usd_total", metrics.cost_usd_total))

    # Derived ratios
    lines.extend(_help_type(
        "shadow_llm_disagreement_rate",
        "DISAGREE_DOWNGRADE verdicts / total invocations.",
        "gauge",
    ))
    lines.append(_line(
        "shadow_llm_disagreement_rate", metrics.disagreement_rate(),
    ))

    lines.extend(_help_type(
        "shadow_llm_abstain_rate",
        "ABSTAIN verdicts / total invocations.",
        "gauge",
    ))
    lines.append(_line("shadow_llm_abstain_rate", metrics.abstain_rate()))

    lines.extend(_help_type(
        "shadow_llm_cache_hit_rate",
        "Cache hits / total invocations.",
        "gauge",
    ))
    lines.append(_line("shadow_llm_cache_hit_rate", metrics.cache_hit_rate()))

    lines.extend(_help_type(
        "shadow_llm_avg_latency_ms",
        "Cold-path average latency in milliseconds.",
        "gauge",
    ))
    lines.append(_line("shadow_llm_avg_latency_ms", metrics.avg_latency_ms()))

    # Reviewer-override counter — ADR-039 §6.3 X.2→X.3 gate.
    lines.extend(_help_type(
        "shadow_llm_reviewer_overrides_of_downgrade_total",
        "Reviewer overrides on cases where L2 LLM Shadow returned "
        "DISAGREE_DOWNGRADE. Feeds the X.2→X.3 ratification gate.",
        "counter",
    ))
    lines.append(_line(
        "shadow_llm_reviewer_overrides_of_downgrade_total",
        metrics.reviewer_overrides_of_llm_downgrade_total,
    ))

    lines.extend(_help_type(
        "shadow_llm_reviewer_override_rate_on_downgrades",
        "Reviewer overrides / DISAGREE_DOWNGRADE verdicts. ADR-039 "
        "§6.3 X.2→X.3 gate target: ≤ 0.35.",
        "gauge",
    ))
    lines.append(_line(
        "shadow_llm_reviewer_override_rate_on_downgrades",
        metrics.reviewer_override_rate_on_llm_downgrades(),
    ))

    return "\n".join(lines) + "\n"


def render_cases_returned_metrics() -> str:
    """Phase 28.5.x §D7 — emit the rolling-window stats on
    /api/v1/cases payload size as a gauge surface. The Grafana
    dashboard plots these against the §28.6 layout; alert fires at
    p99 ≥ 150 (re-open trigger for cursor pagination).
    """
    samples, avg, p95, p99 = cases_returned_snapshot()
    lines: list[str] = []

    lines.extend(_help_type(
        "asoe_cases_returned_samples",
        "Rolling-window sample count for /api/v1/cases payload size.",
        "gauge",
    ))
    lines.append(_line("asoe_cases_returned_samples", samples))

    lines.extend(_help_type(
        "asoe_cases_returned_avg",
        "Rolling-window mean of /api/v1/cases response size.",
        "gauge",
    ))
    lines.append(_line("asoe_cases_returned_avg", avg))

    lines.extend(_help_type(
        "asoe_cases_returned_p95",
        "Rolling-window p95 of /api/v1/cases response size.",
        "gauge",
    ))
    lines.append(_line("asoe_cases_returned_p95", p95))

    lines.extend(_help_type(
        "asoe_cases_returned_p99",
        "Rolling-window p99 of /api/v1/cases response size. Alert fires at >= 150 (re-open cursor pagination per Phase 28.5.x §D7).",
        "gauge",
    ))
    lines.append(_line("asoe_cases_returned_p99", p99))

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Deprecated event_type observer (ADR-034 §6.2 — transitional cutover SLI)
# ---------------------------------------------------------------------------
#
# Two metric families track the legacy `event_type` rename:
#
#   deprecated_event_type_received_total{event_type, deprecated_for}
#     Counter, incremented on every inbound event whose event_type is
#     in the §6.2 legacy alias set. `deprecated_for` carries the
#     ADR-pointer label so the dashboard can group by deadline cohort
#     when multiple aliases live in parallel.
#
#   event_type_received_total{event_type}
#     Counter, incremented on every inbound event regardless of name.
#     Surfaces canonical vs legacy side-by-side on the §28.6 dashboard
#     so producer teams see the trend without log queries.
#
# Both counters are per-process-lifetime; restart resets them. The
# Grafana scrape interval (15s) plus rate() aggregation handles
# discontinuities — same model the asoe-ui CI Prometheus exporter
# (`tests_scenario_failed_total`) uses.

from collections import defaultdict as _defaultdict
from threading import Lock as _EventTypeLock

# `_DEPRECATED_EVENT_TYPES` is the §6.2 alias set with the binding
# ADR pointer label. Adding a new alias to the codebase MUST add
# it here too, paired with the ADR § that authorised it.
_DEPRECATED_EVENT_TYPES: dict[str, str] = {
    "EMAIL_ORDER_ENTRY_REQUEST": "adr-034-§6.2",
    "EMAIL_ORDER":               "adr-034-§6.2",
}

_event_type_lock = _EventTypeLock()
_event_type_counts: dict[str, int] = _defaultdict(int)
_deprecated_event_type_counts: dict[str, int] = _defaultdict(int)


def record_event_type_received(event_type: str) -> None:
    """Record one inbound event's `event_type`. Increments the
    canonical counter, and the deprecation counter when the name
    is in the §6.2 alias set. Called from
    `api/case_resolver.py::resolve_or_open_case` (the inbound-event
    chokepoint for the case-routing path)."""
    with _event_type_lock:
        _event_type_counts[event_type] += 1
        if event_type in _DEPRECATED_EVENT_TYPES:
            _deprecated_event_type_counts[event_type] += 1


def reset_event_type_counters() -> None:
    """Test-only — clears both counter dicts. Production callers
    must not invoke this; Prometheus scraping is monotonic."""
    with _event_type_lock:
        _event_type_counts.clear()
        _deprecated_event_type_counts.clear()


def event_type_snapshot() -> tuple[dict[str, int], dict[str, int]]:
    """Return `(event_type_counts, deprecated_event_type_counts)`
    as copies under the lock. Used by the renderer and by the
    contract tests."""
    with _event_type_lock:
        return dict(_event_type_counts), dict(_deprecated_event_type_counts)


def render_event_type_metrics() -> str:
    """Render `event_type_received_total` and
    `deprecated_event_type_received_total` in Prometheus text
    format. Returns the empty string when no events have been
    observed (avoids zero-cardinality lines on a fresh process)."""
    canonical, deprecated = event_type_snapshot()
    if not canonical and not deprecated:
        return ""
    out: list[str] = []
    out.extend(_help_type(
        "event_type_received_total",
        "Total inbound events grouped by event_type.",
        "counter",
    ))
    for et, count in sorted(canonical.items()):
        out.append(_line(
            "event_type_received_total", count, labels={"event_type": et},
        ))
    out.append("")

    if deprecated:
        out.extend(_help_type(
            "deprecated_event_type_received_total",
            "Inbound events carrying a deprecated event_type "
            "alias. Tracks the ADR-034 §6.2 transitional cutover; "
            "trend goes to zero before the 2026-08-12 deadline.",
            "counter",
        ))
        for et, count in sorted(deprecated.items()):
            out.append(_line(
                "deprecated_event_type_received_total", count,
                labels={
                    "event_type": et,
                    "deprecated_for": _DEPRECATED_EVENT_TYPES[et],
                },
            ))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Ingest → terminal latency SLO histogram (DoR #7).
#
# A classic Prometheus cumulative histogram. `observe_ingest_to_terminal_latency`
# is called once per synchronous resolve (the automated ingest→terminal path)
# with the wall-clock duration in seconds. Buckets are seconds; the implicit
# +Inf bucket catches the tail. Telemetry must never raise into the request
# path, so observe() fails closed on bad input.
# ---------------------------------------------------------------------------

_SLO_BUCKETS_S: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
_slo_lock = _CaseLock()
_slo_bucket_counts: list[int] = [0] * (len(_SLO_BUCKETS_S) + 1)  # last = +Inf
_slo_sum: float = 0.0
_slo_count: int = 0


def observe_ingest_to_terminal_latency(seconds: float) -> None:
    """Record one ingest→terminal latency sample (seconds). No-op on a
    negative / NaN / non-numeric input — never raises (DoR #7)."""
    global _slo_sum, _slo_count
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return
    if s != s or s < 0:  # NaN or negative
        return
    with _slo_lock:
        for i, edge in enumerate(_SLO_BUCKETS_S):
            if s <= edge:
                _slo_bucket_counts[i] += 1
                break
        else:
            _slo_bucket_counts[-1] += 1
        _slo_sum += s
        _slo_count += 1


def reset_slo_histogram() -> None:
    """Test helper — drop all samples."""
    global _slo_sum, _slo_count
    with _slo_lock:
        for i in range(len(_slo_bucket_counts)):
            _slo_bucket_counts[i] = 0
        _slo_sum = 0.0
        _slo_count = 0


def _fmt_le(edge: float) -> str:
    return str(int(edge)) if edge == int(edge) else repr(edge)


def render_ingest_terminal_histogram() -> str:
    name = "asoe_ingest_to_terminal_latency_seconds"
    lines = list(_help_type(
        name, "Wall-clock latency from event ingest to terminal resolve.",
        "histogram",
    ))
    with _slo_lock:
        cumulative = 0
        for i, edge in enumerate(_SLO_BUCKETS_S):
            cumulative += _slo_bucket_counts[i]
            lines.append(_line(f"{name}_bucket", cumulative, labels={"le": _fmt_le(edge)}))
        cumulative += _slo_bucket_counts[-1]
        lines.append(_line(f"{name}_bucket", cumulative, labels={"le": "+Inf"}))
        lines.append(_line(f"{name}_sum", _slo_sum))
        lines.append(_line(f"{name}_count", _slo_count))
    return "\n".join(lines) + "\n"


def render_all() -> str:
    """Convenience entrypoint for the route handler."""
    return (
        render_shadow_llm_metrics(shadow_llm_metrics)
        + render_cases_returned_metrics()
        + render_event_type_metrics()
        + render_ingest_terminal_histogram()
    )
