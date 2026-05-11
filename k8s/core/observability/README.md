# Kubernetes observability (ADR-039 §7.3)

This directory carries **only** the Kubernetes-specific scrape config
(`servicemonitor.yaml`). The dashboard JSON and Prometheus scrape
config are deployment-target-agnostic and live under
[`ops/observability/`](../../../ops/observability/) — same files used
by the Docker Compose stack and by the Azure Managed Grafana
deployment in `infra/main.bicep`.

| File | Location |
|---|---|
| Grafana dashboard JSON | `ops/observability/grafana/dashboards/shadow_llm.json` |
| Prometheus scrape config (Compose / Azure) | `ops/observability/prometheus.yml` |
| **kube-prometheus-stack ServiceMonitor** | `servicemonitor.yaml` (this directory) |

See [`ops/observability/README.md`](../../../ops/observability/README.md)
for the harmonization rationale with Langfuse and the cross-target
deployment guide.

## Apply on Kubernetes

```bash
# ServiceMonitor — picked up automatically by kube-prometheus-stack
# once the `release` label matches the Helm release name.
kubectl apply -f k8s/core/observability/servicemonitor.yaml

# Dashboard — import via the Grafana UI (Dashboards → Import →
# Upload JSON file) or via the Grafana provisioning sidecar by
# adding `ops/observability/grafana/dashboards/shadow_llm.json`
# to the dashboards ConfigMap.
```

## The single-source rule

The Prometheus metric names emitted by `api/metrics.py` are the
contract. Every deployment target reads the same dashboard JSON;
renaming a metric without updating all four sites
(`compliance/shadow_llm.py`, `api/metrics.py`,
`tests/test_metrics_endpoint.py`, dashboard JSON) breaks the panel
without breaking CI. The lock test in `test_metrics_endpoint.py`
exists specifically to catch this.
