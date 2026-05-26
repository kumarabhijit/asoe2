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
    )
