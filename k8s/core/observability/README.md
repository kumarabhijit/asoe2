# asoe-core observability assets

Three files live in this directory:

| File | Purpose |
|---|---|
| `servicemonitor.yaml` | Tells kube-prometheus-stack to scrape `/api/v1/metrics` on the asoe-core Service. |
| `grafana_shadow_llm_dashboard.json` | Importable Grafana dashboard (Grafana 10.x schema v39) plotting the ADR-039 §7.3 SLIs. |
| `README.md` | This file — the harmonization rationale + apply steps. |

## Why Prometheus + Grafana on top of Langfuse

Langfuse and Prometheus serve **different audiences and different
question shapes** for the L2 LLM Compliance Shadow:

| | Langfuse | Prometheus + Grafana |
|---|---|---|
| Granularity | Per-trace (one row per LLM call) | Aggregate (counters / gauges over time windows) |
| Audience | AI engineers, compliance auditors | SRE / on-call rotation |
| Strengths | Prompt versioning, completion content, per-call cost attribution, scoring/feedback | Burn-rate alerting, Alertmanager/PagerDuty hooks, correlation with infra (CPU / latency / error rate), long-retention counter time-series |
| Weaknesses | Weak SLO alerting, no on-call hooks, can't see operator workflow signals | No prompt/completion content, no per-call cost attribution |

Two of the seven SLIs in ADR-039 §7.3 are **not Langfuse-native**:
* `shadow_llm_cache_hit_rate` — happens at our L4 cache, not at the
  LLM call boundary Langfuse sees.
* `shadow_llm_reviewer_override_rate_on_downgrades` — joins L2
  verdict with operator action, recorded long after the LLM call
  returns. This is a workflow metric, not a trace attribute.

For those two **and** for the burn-rate alerting workflow on the rest
of the SLIs, Prometheus is the right tool. For per-trace debugging
("why did the L2 disagree on this specific case?"), Langfuse is the
right tool. We keep both, with a **single source of truth** at the
LLM call site (`compliance.shadow_llm.ShadowLLMMetrics`) and two
projections: Langfuse traces (per-call) + `/api/v1/metrics` (aggregate).

## Drill-down: Grafana → Langfuse

Every panel in `grafana_shadow_llm_dashboard.json` carries a `links[]`
entry pointing back to a Langfuse trace search filtered by the
panel's dimension and the dashboard's current time window. The
on-call workflow is:

1. Alertmanager pages "shadow_llm_disagreement_rate > 0.30 for 15m".
2. On-call opens the Grafana dashboard via the alert link.
3. The "Disagreement + abstain rate" panel shows the spike.
4. Click the panel's "Langfuse — traces driving disagreement spike"
   link. Langfuse opens, pre-filtered to the same time window with
   `metadata.verdict = DISAGREE_DOWNGRADE`.
5. On-call drills into individual traces to diagnose.

Set `LANGFUSE_BASE_URL` + `LANGFUSE_PROJECT` (dashboard variables)
per environment so the deep-links resolve to the right Langfuse
instance.

## Authoritative cost source

The `shadow_llm_cost_usd_total` Prometheus gauge and the per-call
Langfuse cost attribute are both populated from the same
`ShadowLLMMetrics.cost_usd_total` accumulator (in
`compliance/shadow_llm.py`). They are recorded **once** at the
provider call site, never re-derived. If you find a discrepancy
between the two, the call site is the bug — fix it there, not in
the dashboard panel or the Langfuse tag.

## Apply

```bash
# ServiceMonitor — picked up automatically once kube-prometheus-stack
# is installed in the cluster with matching `release` label.
kubectl apply -f k8s/core/observability/servicemonitor.yaml

# Grafana dashboard — import via the UI (Dashboards → Import →
# Upload JSON file) or via the Grafana provisioning sidecar by
# dropping the JSON into the dashboards ConfigMap volume.
```

## Updating the dashboard

When ADR-039 §7.3 grows a new SLI:

1. Add the counter/gauge to `compliance/shadow_llm.py::ShadowLLMMetrics`.
2. Emit it in `api/metrics.py::render_shadow_llm_metrics`.
3. Lock the metric name in `tests/test_metrics_endpoint.py::test_emits_required_metric_families` so a rename here fails CI before it silently empties the dashboard panel.
4. Add a Grafana panel with a Langfuse drill-down link.
5. Bump the dashboard `version` field in the JSON.

The Prometheus metric name is the contract. Don't rename without
updating all four sites in the same PR.
