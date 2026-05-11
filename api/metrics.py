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


def render_all() -> str:
    """Convenience entrypoint for the route handler."""
    return render_shadow_llm_metrics(shadow_llm_metrics)
