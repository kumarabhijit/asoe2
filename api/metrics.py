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


# ---------------------------------------------------------------------------
# Gateway-tier metering + circuit-breaker state (DoR #8 — parity with the LLM
# tier). Per-gateway counters; the breaker state is exported as a labelled
# gauge (0=closed, 1=half_open, 2=open) for alerting on a tripped dependency.
# ---------------------------------------------------------------------------

_BREAKER_STATE_CODE = {"closed": 0, "half_open": 1, "open": 2}


def render_gateway_metrics() -> str:
    # Lazy import keeps the metrics module importable without pulling the
    # gateway stack at app boot, and avoids any import cycle.
    from gateways.circuit_breaker import breaker_snapshots, metering_snapshot

    meters = metering_snapshot()
    breakers = breaker_snapshots()
    lines: list[str] = []

    lines += _help_type("gateway_calls_total", "Gateway calls that reached the dependency.", "counter")
    for name, m in sorted(meters.items()):
        lines.append(_line("gateway_calls_total", m.calls, labels={"gateway": name}))

    lines += _help_type("gateway_call_failures_total", "Gateway calls that failed (timeout/exception/error status).", "counter")
    for name, m in sorted(meters.items()):
        lines.append(_line("gateway_call_failures_total", m.failures, labels={"gateway": name}))

    lines += _help_type("gateway_short_circuits_total", "Gateway calls rejected because the circuit breaker was OPEN.", "counter")
    for name, m in sorted(meters.items()):
        lines.append(_line("gateway_short_circuits_total", m.short_circuits, labels={"gateway": name}))

    lines += _help_type("gateway_latency_ms_sum", "Cumulative gateway call latency (ms).", "counter")
    for name, m in sorted(meters.items()):
        lines.append(_line("gateway_latency_ms_sum", m.latency_ms_sum, labels={"gateway": name}))

    lines += _help_type("gateway_circuit_breaker_state", "Gateway circuit breaker state (0=closed,1=half_open,2=open).", "gauge")
    for name, snap in sorted(breakers.items()):
        code = _BREAKER_STATE_CODE.get(snap.state.value, 0)
        lines.append(_line("gateway_circuit_breaker_state", code, labels={"gateway": name}))

    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# Automation-bias SLIs (DoR #11). The reviewer-override rate already lives on
# the shadow-LLM surface; these add the two behavioural signals that detect
# rubber-stamping: the Layer-2-open rate (did the operator expand the evidence
# before deciding?) and decision dwell (how long from opening the case to acting).
# Fed by POST /api/v1/metrics/reviewer-activity at disposition time.
# ---------------------------------------------------------------------------

_DWELL_BUCKETS_S: tuple[float, ...] = (1, 3, 5, 10, 30, 60, 300)
_ab_lock = _CaseLock()
_ab_decisions: int = 0
_ab_layer2_opened: int = 0
_ab_dwell_buckets: list[int] = [0] * (len(_DWELL_BUCKETS_S) + 1)
_ab_dwell_sum: float = 0.0
# ADR-043 §2.7 / D12 — decision-quality cohort: scrutiny split by whether an
# in-document evidence highlight was shown to the operator. Bounded to a 2-way
# {true,false} label so a *drop* in scrutiny when highlighting is on registers
# as a regression rather than a speed win.
_ab_decisions_by_hl: dict[bool, int] = {True: 0, False: 0}
_ab_layer2_by_hl: dict[bool, int] = {True: 0, False: 0}


def record_reviewer_activity(
    *, dwell_ms: float, layer2_opened: bool, highlight_shown: bool = False,
) -> None:
    """Record one operator decision's automation-bias signals. No-op on a
    negative / NaN / non-numeric dwell — never raises (DoR #11). The
    ``highlight_shown`` cohort (ADR-043 §2.7) lets the Layer-2-open rate be
    compared with vs without an evidence highlight on screen."""
    global _ab_decisions, _ab_layer2_opened, _ab_dwell_sum
    try:
        dwell_s = float(dwell_ms) / 1000.0
    except (TypeError, ValueError):
        return
    if dwell_s != dwell_s or dwell_s < 0:  # NaN or negative
        return
    hl = bool(highlight_shown)
    with _ab_lock:
        _ab_decisions += 1
        _ab_decisions_by_hl[hl] += 1
        if layer2_opened:
            _ab_layer2_opened += 1
            _ab_layer2_by_hl[hl] += 1
        for i, edge in enumerate(_DWELL_BUCKETS_S):
            if dwell_s <= edge:
                _ab_dwell_buckets[i] += 1
                break
        else:
            _ab_dwell_buckets[-1] += 1
        _ab_dwell_sum += dwell_s


def reset_reviewer_activity() -> None:
    """Test helper — drop all automation-bias samples."""
    global _ab_decisions, _ab_layer2_opened, _ab_dwell_sum
    with _ab_lock:
        _ab_decisions = 0
        _ab_layer2_opened = 0
        for i in range(len(_ab_dwell_buckets)):
            _ab_dwell_buckets[i] = 0
        _ab_dwell_sum = 0.0
        for k in (True, False):
            _ab_decisions_by_hl[k] = 0
            _ab_layer2_by_hl[k] = 0


def render_reviewer_activity_metrics() -> str:
    lines: list[str] = []
    with _ab_lock:
        decisions = _ab_decisions
        opened = _ab_layer2_opened
        buckets = list(_ab_dwell_buckets)
        dwell_sum = _ab_dwell_sum
        decisions_by_hl = dict(_ab_decisions_by_hl)
        layer2_by_hl = dict(_ab_layer2_by_hl)
    lines += _help_type("reviewer_decisions_total", "Operator disposition decisions observed.", "counter")
    lines.append(_line("reviewer_decisions_total", decisions))
    lines += _help_type("reviewer_layer2_opened_total", "Decisions where the operator expanded Layer-2 evidence first.", "counter")
    lines.append(_line("reviewer_layer2_opened_total", opened))
    lines += _help_type("reviewer_layer2_open_rate", "Layer-2-open rate (opened / decisions); low values flag automation bias.", "gauge")
    lines.append(_line("reviewer_layer2_open_rate", round(opened / decisions, 4) if decisions else 0.0))
    # Decision-quality cohort (ADR-043 §2.7) — decisions + Layer-2-open rate
    # split by highlight_shown so scrutiny can be A/B-compared.
    lines += _help_type(
        "reviewer_decisions_by_highlight_total",
        "Operator decisions split by whether an evidence highlight was shown "
        "(ADR-043 §2.7 decision-quality cohort).",
        "counter",
    )
    for shown in (True, False):
        lines.append(_line(
            "reviewer_decisions_by_highlight_total", decisions_by_hl[shown],
            labels={"highlight_shown": "true" if shown else "false"},
        ))
    lines += _help_type(
        "reviewer_layer2_open_rate_by_highlight",
        "Layer-2-open rate by highlight cohort; a drop under highlight_shown="
        "true vs false is the automation-bias regression signal (ADR-043 §2.7).",
        "gauge",
    )
    for shown in (True, False):
        cohort_dec = decisions_by_hl[shown]
        rate = round(layer2_by_hl[shown] / cohort_dec, 4) if cohort_dec else 0.0
        lines.append(_line(
            "reviewer_layer2_open_rate_by_highlight", rate,
            labels={"highlight_shown": "true" if shown else "false"},
        ))
    lines += _help_type("reviewer_decision_dwell_seconds", "Time from opening a case to acting on it.", "histogram")
    name = "reviewer_decision_dwell_seconds"
    cumulative = 0
    for i, edge in enumerate(_DWELL_BUCKETS_S):
        cumulative += buckets[i]
        lines.append(_line(f"{name}_bucket", cumulative, labels={"le": _fmt_le(edge)}))
    cumulative += buckets[-1]
    lines.append(_line(f"{name}_bucket", cumulative, labels={"le": "+Inf"}))
    lines.append(_line(f"{name}_sum", dwell_sum))
    lines.append(_line(f"{name}_count", decisions))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Attachment-preview & evidence-highlighting SLIs (ADR-043 §2.6). The panel's
# #1 hazard is a highlight that silently lands wrong; `highlight_outcome_total`
# (located|unlocated|ambiguous) is the leading indicator that a PDF.js bump
# broke positioning. Labels are deliberately BOUNDED to {result, mime} — no
# attachment_id / case_id (cardinality bomb); per-document detail belongs on the
# structured event/trace, never on a Prometheus label.
# ---------------------------------------------------------------------------

_PREVIEW_LATENCY_BUCKETS_MS: tuple[float, ...] = (50, 100, 250, 500, 1000, 2500, 5000)
_HIGHLIGHT_RESULTS = ("located", "unlocated", "ambiguous")
_PREVIEW_RESULTS = ("ok", "error", "unsupported")
_PREVIEW_MIMES = ("pdf", "image", "text", "csv", "other")

_preview_lock = _CaseLock()
_highlight_outcomes: dict[tuple[str, str], int] = _defaultdict(int)  # (result, mime)
_preview_renders: dict[tuple[str, str], int] = _defaultdict(int)     # (result, mime)
_preview_latency_buckets: list[int] = [0] * (len(_PREVIEW_LATENCY_BUCKETS_MS) + 1)
_preview_latency_sum: float = 0.0


def _bounded_mime(mime: str) -> str:
    return mime if mime in _PREVIEW_MIMES else "other"


def record_highlight_outcome(*, result: str, mime: str) -> None:
    """Record one anchor's render outcome (located|unlocated|ambiguous). No-op
    on an out-of-vocabulary result — never raises into the request path."""
    if result not in _HIGHLIGHT_RESULTS:
        return
    with _preview_lock:
        _highlight_outcomes[(result, _bounded_mime(mime))] += 1


def record_preview_render(*, result: str, mime: str, latency_ms: float) -> None:
    """Record one attachment preview render + its latency. No-op on a bad
    result or non-numeric / negative latency."""
    global _preview_latency_sum
    if result not in _PREVIEW_RESULTS:
        return
    with _preview_lock:
        _preview_renders[(result, _bounded_mime(mime))] += 1
        try:
            ms = float(latency_ms)
        except (TypeError, ValueError):
            return
        if ms != ms or ms < 0:  # NaN or negative
            return
        for i, edge in enumerate(_PREVIEW_LATENCY_BUCKETS_MS):
            if ms <= edge:
                _preview_latency_buckets[i] += 1
                break
        else:
            _preview_latency_buckets[-1] += 1
        _preview_latency_sum += ms


def reset_preview_metrics() -> None:
    """Test helper — drop all preview/highlight samples."""
    global _preview_latency_sum
    with _preview_lock:
        _highlight_outcomes.clear()
        _preview_renders.clear()
        for i in range(len(_preview_latency_buckets)):
            _preview_latency_buckets[i] = 0
        _preview_latency_sum = 0.0


def render_preview_metrics() -> str:
    with _preview_lock:
        highlights = dict(_highlight_outcomes)
        renders = dict(_preview_renders)
        buckets = list(_preview_latency_buckets)
        latency_sum = _preview_latency_sum

    lines: list[str] = []
    lines += _help_type(
        "highlight_outcome_total",
        "Evidence-highlight render outcomes by result and document type. "
        "A low located-ratio (or any ambiguous) is the leading signal that "
        "positioning regressed (ADR-043 §2.6).",
        "counter",
    )
    for (result, mime), count in sorted(highlights.items()):
        lines.append(_line("highlight_outcome_total", count, labels={"result": result, "mime": mime}))

    lines += _help_type(
        "preview_render_total",
        "Attachment preview renders by result and document type.",
        "counter",
    )
    for (result, mime), count in sorted(renders.items()):
        lines.append(_line("preview_render_total", count, labels={"result": result, "mime": mime}))

    name = "preview_render_latency_ms"
    lines += _help_type(name, "Attachment preview render latency (ms).", "histogram")
    cumulative = 0
    for i, edge in enumerate(_PREVIEW_LATENCY_BUCKETS_MS):
        cumulative += buckets[i]
        lines.append(_line(f"{name}_bucket", cumulative, labels={"le": _fmt_le(edge)}))
    cumulative += buckets[-1]
    lines.append(_line(f"{name}_bucket", cumulative, labels={"le": "+Inf"}))
    lines.append(_line(f"{name}_sum", latency_sum))
    lines.append(_line(f"{name}_count", cumulative))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Spatial document-extraction cost + drift (ADR-045 §2.5). Cost must be
# attributable (labelled by tenant / provider / model_id), not merely capped by
# the policy guardrail. The drift series (mean confidence + canary containment,
# keyed by model_id + prompt_hash) makes a quiet model regression visible
# between nightly evals. Label cardinality is bounded by the customer × provider
# × model_id space (not per-document) so this stays Prometheus-safe.
# ---------------------------------------------------------------------------

_extraction_lock = _CaseLock()
_extraction_cost_usd: dict[tuple, float] = _defaultdict(float)   # (tenant, provider, model_id)
_extraction_pages: dict[tuple, int] = _defaultdict(int)          # (tenant, provider, model_id)
_extraction_conf: dict[tuple, list] = _defaultdict(list)         # (model_id, prompt_hash) -> [conf]
_extraction_contain: dict[tuple, list] = _defaultdict(list)      # (model_id, prompt_hash) -> [containment]


def record_extraction_cost(
    *, tenant: str, provider: str, model_id: str, pages: int, cost_usd: float,
) -> None:
    """Record one document-extraction call's pages + cost. No-op on a negative /
    non-numeric cost — never raises into the extraction path."""
    try:
        cost = float(cost_usd)
        n_pages = int(pages)
    except (TypeError, ValueError):
        return
    if cost != cost or cost < 0 or n_pages < 0:  # NaN / negative
        return
    key = (tenant, provider, model_id)
    with _extraction_lock:
        _extraction_cost_usd[key] += cost
        _extraction_pages[key] += n_pages


def record_extraction_drift(
    *, model_id: str, prompt_hash: str, confidence: float, containment: float,
) -> None:
    """Record a per-extraction (or canary) confidence + containment sample for
    the drift series. No-op on non-numeric input."""
    try:
        conf = float(confidence)
        contain = float(containment)
    except (TypeError, ValueError):
        return
    key = (model_id, prompt_hash)
    with _extraction_lock:
        _extraction_conf[key].append(conf)
        _extraction_contain[key].append(contain)


def reset_extraction_metrics() -> None:
    """Test helper — drop all extraction cost/drift samples."""
    with _extraction_lock:
        _extraction_cost_usd.clear()
        _extraction_pages.clear()
        _extraction_conf.clear()
        _extraction_contain.clear()


def snapshot_extraction_metrics() -> dict:
    """Return a read-only snapshot of the extraction metric stores.

    Used by the drift-alert integration (api/observability/drift_alert_integration.py)
    to evaluate the recent-vs-baseline split without locking the
    collector for the duration of the comparison.
    """
    with _extraction_lock:
        return {
            "cost_usd": dict(_extraction_cost_usd),
            "pages": dict(_extraction_pages),
            "conf": {k: list(v) for k, v in _extraction_conf.items()},
            "contain": {k: list(v) for k, v in _extraction_contain.items()},
        }


def render_extraction_metrics() -> str:
    with _extraction_lock:
        cost = dict(_extraction_cost_usd)
        pages = dict(_extraction_pages)
        conf = {k: list(v) for k, v in _extraction_conf.items()}
        contain = {k: list(v) for k, v in _extraction_contain.items()}

    lines: list[str] = []
    lines += _help_type(
        "extraction_cost_usd_total",
        "Cumulative document-extraction spend, attributable by tenant / provider "
        "/ model_id (ADR-045 §2.5).",
        "counter",
    )
    for (tenant, provider, model_id), value in sorted(cost.items()):
        lines.append(_line(
            "extraction_cost_usd_total", round(value, 6),
            labels={"tenant": tenant, "provider": provider, "model_id": model_id},
        ))
    lines += _help_type(
        "extraction_pages_total",
        "Cumulative pages sent to document extraction, by tenant / provider / model_id.",
        "counter",
    )
    for (tenant, provider, model_id), value in sorted(pages.items()):
        lines.append(_line(
            "extraction_pages_total", value,
            labels={"tenant": tenant, "provider": provider, "model_id": model_id},
        ))
    lines += _help_type(
        "extraction_mean_confidence",
        "Mean per-anchor confidence by model_id / prompt_hash — drift signal "
        "(ADR-045 §2.5); alert on shift between nightly evals.",
        "gauge",
    )
    for (model_id, prompt_hash), samples in sorted(conf.items()):
        if samples:
            lines.append(_line(
                "extraction_mean_confidence", round(sum(samples) / len(samples), 6),
                labels={"model_id": model_id, "prompt_hash": prompt_hash},
            ))
    lines += _help_type(
        "extraction_canary_containment",
        "Mean canary containment by model_id / prompt_hash — drift signal "
        "(ADR-045 §2.5).",
        "gauge",
    )
    for (model_id, prompt_hash), samples in sorted(contain.items()):
        if samples:
            lines.append(_line(
                "extraction_canary_containment", round(sum(samples) / len(samples), 6),
                labels={"model_id": model_id, "prompt_hash": prompt_hash},
            ))
    return "\n".join(lines) + "\n" if lines else ""


def render_all() -> str:
    """Convenience entrypoint for the route handler."""
    return (
        render_shadow_llm_metrics(shadow_llm_metrics)
        + render_cases_returned_metrics()
        + render_event_type_metrics()
        + render_ingest_terminal_histogram()
        + render_gateway_metrics()
        + render_reviewer_activity_metrics()
        + render_preview_metrics()
        + render_extraction_metrics()
        + render_cases_v2_telemetry_metrics()
    )


# ---------------------------------------------------------------------------
# ADR-041 P3e Phase 3 sign-off gate #5 — cases V2 telemetry counters
# ---------------------------------------------------------------------------
# Three best-effort metrics the gate requires:
#
#   * row-click             — operators picking from the queue. Used
#                              by gate review to confirm SLA-sort
#                              effectiveness (do they click the top
#                              row?).
#   * time-to-first-action  — dwell from case-open to first HITL
#                              button click. Compared with the legacy
#                              row baseline by gate review.
#   * analysis-scroll-depth — how far into Agent Analysis operators
#                              read before clicking. Sub-gate metric
#                              for the Compliance audit-format review.
#
# All three are tenant-agnostic counters (no PII; aggregated). Lock-
# protected for thread safety in the FastAPI worker pool. The
# Prometheus exposition format is consistent with the existing
# reviewer-activity counters above so dashboards can re-use the
# render template.

from threading import Lock as _CasesV2Lock  # noqa: E402
from typing import Dict, List, Optional  # noqa: E402, F811

_cases_v2_lock = _CasesV2Lock()

# Aggregate counters
_cases_v2_row_clicks: int = 0
_cases_v2_row_clicks_by_verdict: Dict[str, int] = {
    "R": 0, "A": 0, "G": 0, "none": 0,
}

# Dwell histogram for time-to-first-action. Re-uses the existing
# automation-bias bucket edges (0.5s, 1s, 2s, 5s, 10s, 30s, 60s, 120s)
# so the gate-review dashboards can overlay the two distributions.
_cases_v2_ttfa_by_action: Dict[str, int] = {
    "approve": 0, "reject": 0, "override": 0,
    "escalate": 0, "reanalyze": 0,
}
_cases_v2_ttfa_dwell_buckets: List[int] = [0] * (len(_DWELL_BUCKETS_S) + 1)
_cases_v2_ttfa_dwell_sum: float = 0.0
_cases_v2_ttfa_count: int = 0

# Dwell histogram for time-to-action-submit (S2 sprint 2026-05-28
# finding #8). Distinct from time-to-first-action: this is the dwell
# from case-open to the moment the operator CONFIRMS an action (after
# typing any comment + picking any mandatory reason_tag), so the delta
# between the two distributions is the comment-dialog dwell. Re-uses
# the same automation-bias bucket edges so the gate-review dashboards
# can overlay all three. `with_reason_tag` is a bounded-cardinality
# count (present vs absent) — per-tag series are deliberately NOT
# emitted to avoid Prometheus label explosion on open-ended tags.
_cases_v2_ttas_by_action: Dict[str, int] = {
    "approve": 0, "reject": 0, "reanalyze": 0,
}
_cases_v2_ttas_dwell_buckets: List[int] = [0] * (len(_DWELL_BUCKETS_S) + 1)
_cases_v2_ttas_dwell_sum: float = 0.0
_cases_v2_ttas_count: int = 0
_cases_v2_ttas_with_reason_tag: int = 0

# Analysis-scroll-depth histogram. 21 buckets matching the V2
# observer's threshold sweep (5% resolution).
_SCROLL_BUCKETS = 21
_cases_v2_scroll_buckets: List[int] = [0] * _SCROLL_BUCKETS
_cases_v2_scroll_count: int = 0
_cases_v2_scroll_acted: int = 0


def record_cases_row_click(verdict: Optional[str] = None) -> None:
    """One queue-row click. `verdict` is the audit_verdict_color
    on the row at click time (R/A/G/None). Best-effort; never
    raises."""
    global _cases_v2_row_clicks
    key = verdict if verdict in ("R", "A", "G") else "none"
    with _cases_v2_lock:
        _cases_v2_row_clicks += 1
        _cases_v2_row_clicks_by_verdict[key] = (
            _cases_v2_row_clicks_by_verdict.get(key, 0) + 1
        )


def record_cases_time_to_first_action(
    *, dwell_ms: float, first_action: str,
) -> None:
    """One time-to-first-action sample. `first_action` is one of
    approve / reject / override / escalate / reanalyze. No-op on
    a negative / NaN / non-numeric dwell (matches the
    reviewer-activity contract). Unknown `first_action` is
    counted under no per-action key but still contributes to the
    histogram so the metric never silently drops samples."""
    global _cases_v2_ttfa_dwell_sum, _cases_v2_ttfa_count
    try:
        dwell_s = float(dwell_ms) / 1000.0
    except (TypeError, ValueError):
        return
    if dwell_s != dwell_s or dwell_s < 0:  # NaN or negative
        return
    with _cases_v2_lock:
        if first_action in _cases_v2_ttfa_by_action:
            _cases_v2_ttfa_by_action[first_action] += 1
        for i, edge in enumerate(_DWELL_BUCKETS_S):
            if dwell_s <= edge:
                _cases_v2_ttfa_dwell_buckets[i] += 1
                break
        else:
            _cases_v2_ttfa_dwell_buckets[-1] += 1
        _cases_v2_ttfa_dwell_sum += dwell_s
        _cases_v2_ttfa_count += 1


def record_cases_time_to_action_submit(
    *, dwell_ms: float, action: str, reason_tag: Optional[str] = None,
) -> None:
    """One time-to-action-submit sample. `action` is one of
    approve / reject / reanalyze (the comment-dialog action set;
    override / escalate are one-step and report through their own
    channels). No-op on a negative / NaN / non-numeric dwell
    (matches the time-to-first-action contract). Unknown `action`
    still contributes to the histogram so the metric never
    silently drops samples. `reason_tag` only increments a bounded
    present/absent counter — its open-ended value is never used as
    a Prometheus label."""
    global _cases_v2_ttas_dwell_sum, _cases_v2_ttas_count
    global _cases_v2_ttas_with_reason_tag
    try:
        dwell_s = float(dwell_ms) / 1000.0
    except (TypeError, ValueError):
        return
    if dwell_s != dwell_s or dwell_s < 0:  # NaN or negative
        return
    with _cases_v2_lock:
        if action in _cases_v2_ttas_by_action:
            _cases_v2_ttas_by_action[action] += 1
        for i, edge in enumerate(_DWELL_BUCKETS_S):
            if dwell_s <= edge:
                _cases_v2_ttas_dwell_buckets[i] += 1
                break
        else:
            _cases_v2_ttas_dwell_buckets[-1] += 1
        _cases_v2_ttas_dwell_sum += dwell_s
        _cases_v2_ttas_count += 1
        if reason_tag:
            _cases_v2_ttas_with_reason_tag += 1


def record_cases_analysis_scroll_depth(
    *, max_scroll_pct: float, acted: bool,
) -> None:
    """One scroll-depth sample. `max_scroll_pct` is clamped to
    [0, 1] before bucketing. `acted=True` flags samples where the
    operator clicked an action after observing the reported
    max depth — the gate-review correlation. Best-effort; never
    raises."""
    global _cases_v2_scroll_count, _cases_v2_scroll_acted
    try:
        pct = float(max_scroll_pct)
    except (TypeError, ValueError):
        return
    if pct != pct:  # NaN
        return
    if pct < 0:
        pct = 0.0
    if pct > 1:
        pct = 1.0
    # bucket = floor(pct * (N-1)); pct=1.0 → top bucket inclusive.
    bucket = min(int(pct * (_SCROLL_BUCKETS - 1) + 0.5), _SCROLL_BUCKETS - 1)
    with _cases_v2_lock:
        _cases_v2_scroll_buckets[bucket] += 1
        _cases_v2_scroll_count += 1
        if acted:
            _cases_v2_scroll_acted += 1


def reset_cases_v2_telemetry() -> None:
    """Test helper — drop all V2 telemetry samples."""
    global _cases_v2_row_clicks, _cases_v2_ttfa_dwell_sum
    global _cases_v2_ttfa_count, _cases_v2_scroll_count, _cases_v2_scroll_acted
    global _cases_v2_ttas_dwell_sum, _cases_v2_ttas_count
    global _cases_v2_ttas_with_reason_tag
    with _cases_v2_lock:
        _cases_v2_row_clicks = 0
        for k in _cases_v2_row_clicks_by_verdict:
            _cases_v2_row_clicks_by_verdict[k] = 0
        for k in _cases_v2_ttfa_by_action:
            _cases_v2_ttfa_by_action[k] = 0
        for i in range(len(_cases_v2_ttfa_dwell_buckets)):
            _cases_v2_ttfa_dwell_buckets[i] = 0
        _cases_v2_ttfa_dwell_sum = 0.0
        _cases_v2_ttfa_count = 0
        for k in _cases_v2_ttas_by_action:
            _cases_v2_ttas_by_action[k] = 0
        for i in range(len(_cases_v2_ttas_dwell_buckets)):
            _cases_v2_ttas_dwell_buckets[i] = 0
        _cases_v2_ttas_dwell_sum = 0.0
        _cases_v2_ttas_count = 0
        _cases_v2_ttas_with_reason_tag = 0
        for i in range(_SCROLL_BUCKETS):
            _cases_v2_scroll_buckets[i] = 0
        _cases_v2_scroll_count = 0
        _cases_v2_scroll_acted = 0


def render_cases_v2_telemetry_metrics() -> str:
    """Prometheus text rendering for the three counters."""
    lines: List[str] = []
    with _cases_v2_lock:
        lines.append(
            "# HELP cases_v2_row_clicks_total Cases V2 queue-row click events."
        )
        lines.append("# TYPE cases_v2_row_clicks_total counter")
        lines.append(f"cases_v2_row_clicks_total {_cases_v2_row_clicks}")
        for verdict, count in _cases_v2_row_clicks_by_verdict.items():
            lines.append(
                f'cases_v2_row_clicks_by_verdict_total'
                f'{{verdict="{verdict}"}} {count}'
            )

        lines.append(
            "# HELP cases_v2_time_to_first_action_total "
            "Cases V2 time-to-first-action samples."
        )
        lines.append("# TYPE cases_v2_time_to_first_action_total counter")
        lines.append(
            f"cases_v2_time_to_first_action_total {_cases_v2_ttfa_count}"
        )
        for action, count in _cases_v2_ttfa_by_action.items():
            lines.append(
                f'cases_v2_time_to_first_action_by_action_total'
                f'{{action="{action}"}} {count}'
            )
        lines.append(
            f"cases_v2_time_to_first_action_dwell_sum_seconds "
            f"{_cases_v2_ttfa_dwell_sum:.3f}"
        )

        lines.append(
            "# HELP cases_v2_time_to_action_submit_total "
            "Cases V2 time-to-action-submit samples (case-open to "
            "confirm, post comment + reason_tag)."
        )
        lines.append("# TYPE cases_v2_time_to_action_submit_total counter")
        lines.append(
            f"cases_v2_time_to_action_submit_total {_cases_v2_ttas_count}"
        )
        for action, count in _cases_v2_ttas_by_action.items():
            lines.append(
                f'cases_v2_time_to_action_submit_by_action_total'
                f'{{action="{action}"}} {count}'
            )
        lines.append(
            f"cases_v2_time_to_action_submit_with_reason_tag_total "
            f"{_cases_v2_ttas_with_reason_tag}"
        )
        lines.append(
            f"cases_v2_time_to_action_submit_dwell_sum_seconds "
            f"{_cases_v2_ttas_dwell_sum:.3f}"
        )

        lines.append(
            "# HELP cases_v2_analysis_scroll_depth_total "
            "Cases V2 analysis-scroll-depth samples."
        )
        lines.append("# TYPE cases_v2_analysis_scroll_depth_total counter")
        lines.append(
            f"cases_v2_analysis_scroll_depth_total {_cases_v2_scroll_count}"
        )
        lines.append(
            f"cases_v2_analysis_scroll_depth_acted_total {_cases_v2_scroll_acted}"
        )
    return "\n".join(lines) + "\n"
