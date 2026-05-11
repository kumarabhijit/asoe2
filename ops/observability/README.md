# ASOE observability — multi-target deployment

The L2 LLM Compliance Shadow's ADR-039 §7.3 SLI surface deploys to
three targets from a single set of canonical assets:

```
ops/observability/
├── prometheus.yml                          # scrape config
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/prometheus.yaml     # Grafana datasource (Prometheus)
│   │   └── dashboards/asoe.yaml            # provider config (auto-import)
│   └── dashboards/
│       └── shadow_llm.json                 # the §7.3 dashboard JSON
└── README.md                               # this file
```

| Deployment target | Compose / image | Where the canonical assets are picked up |
|---|---|---|
| Local `docker compose` | `docker-compose.observability.yml` overlay (prom/prometheus + grafana/grafana) | Bind-mounted from `ops/observability/` |
| Azure Container Apps | `Dockerfile.prometheus` + `Dockerfile.grafana` + `infra/main.bicep` (`deployObservability=true`) | `COPY` baked into the images at build time |
| Kubernetes | kube-prometheus-stack `ServiceMonitor` + Grafana sidecar | `servicemonitor.yaml` references the same metric paths; dashboard JSON mounted via ConfigMap |

## The harmonization rationale

The project already uses **Langfuse** for per-trace LLM observability
(prompt + completion + per-call cost — AI engineer + compliance
auditor audience). Prometheus + Grafana adds the **aggregate SLI
surface** for the on-call rotation. Two tools, different question
shapes:

| | Langfuse | Prometheus + Grafana |
|---|---|---|
| Granularity | Per-trace (one row per LLM call) | Aggregate (counters / gauges over time windows) |
| Strengths | Prompt versioning, completion content, per-call cost attribution, scoring | Burn-rate alerting, Alertmanager/PagerDuty, infra correlation |
| Weaknesses | Weak SLO alerting, no on-call hooks, can't see operator workflow signals | No prompt/completion content, no per-call cost attribution |

Two of the seven SLIs in ADR-039 §7.3 are **not Langfuse-native**:
- `shadow_llm_cache_hit_rate` happens at our L4 cache, not at the LLM
  call boundary Langfuse sees.
- `shadow_llm_reviewer_override_rate_on_downgrades` joins L2 verdict
  with operator action recorded long after the LLM call returns.

We keep both with a **single source of truth** at the LLM call site
(`compliance/shadow_llm.py::ShadowLLMMetrics`) and two projections:
Langfuse traces (per-call) + `/api/v1/metrics` (aggregate). Every
Grafana panel carries a `links[]` deep-link back to a Langfuse trace
search filtered by the panel's dimension and the dashboard's current
time window, so on-call drills from "disagreement spike at 14:32" to
individual traces in one click.

## Local — `docker compose`

```bash
# Bring up the base stack plus the observability overlay.
docker compose -f docker-compose.yml -f docker-compose.observability.yml up

# Then:
#   http://localhost:8000/api/v1/metrics   raw Prometheus text
#   http://localhost:9090/targets          Prometheus target status
#   http://localhost:3001/                 Grafana (admin / admin)
```

The Grafana dashboard "ASOE — L2 LLM Shadow SLIs (ADR-039 §7.3)"
auto-provisions on first start. Edit
`ops/observability/grafana/dashboards/shadow_llm.json` and the
provider re-imports within 30s (`updateIntervalSeconds: 30` in
`asoe.yaml`).

Override the Langfuse deep-links per environment:

```bash
LANGFUSE_BASE_URL=https://langfuse.internal.acme.com \
LANGFUSE_PROJECT=asoe-prod \
docker compose -f docker-compose.yml -f docker-compose.observability.yml up
```

## Azure Container Apps

The Bicep template extends with two thin Container Apps
(`asoe-prom`, `asoe-graf`) gated behind a `deployObservability`
parameter. Both images bake in the canonical configs at build time
so there are no Azure Files mounts to manage.

**Step 1 — build + push the images:**

```bash
az acr build -r asoepreprodacr -f Dockerfile.prometheus \
    -t asoe-prometheus:${GIT_SHA} .

az acr build -r asoepreprodacr -f Dockerfile.grafana \
    -t asoe-grafana:${GIT_SHA} .
```

**Step 2 — deploy the stack:**

```bash
az deployment group create \
    -g asoe-preprod-rg \
    -f infra/main.bicep \
    -p @infra/parameters.sandbox.json \
    -p deployContainerApp=true \
    -p deployObservability=true \
    -p prometheusImage=asoepreprodacr.azurecr.io/asoe-prometheus:${GIT_SHA} \
    -p grafanaImage=asoepreprodacr.azurecr.io/asoe-grafana:${GIT_SHA} \
    -p grafanaAdminPassword=$(openssl rand -base64 24)
```

**Step 3 — open Grafana:**

The deployment outputs `grafanaAppFqdn`. Hit
`https://<grafanaAppFqdn>/` and sign in with `admin` + the password
you passed in. The dashboard is pre-imported under the "ASOE"
folder.

The Prometheus Container App is internal-only; Grafana reaches it
over the managed-environment private network at
`http://<prometheusAppName>.internal.<defaultDomain>:9090`. There
is no public ingress for Prometheus.

## Kubernetes

See [`k8s/core/observability/`](../../k8s/core/observability/) —
that directory carries the kube-prometheus-stack `ServiceMonitor`
that's the only file specific to the k8s path. The dashboard JSON
and Prometheus scrape config (this directory) are reused.

## Authoritative cost source

The `shadow_llm_cost_usd_total` Prometheus gauge and the Langfuse
per-call cost attribute are both populated from
`ShadowLLMMetrics.cost_usd_total` — recorded once at the provider
call site, never re-derived. If you find a discrepancy between the
two, the call site is the bug — fix it there, not in the dashboard
panel or the Langfuse tag.

## Updating the dashboard

When ADR-039 §7.3 grows a new SLI:

1. Add the counter/gauge to `compliance/shadow_llm.py::ShadowLLMMetrics`.
2. Emit it in `api/metrics.py::render_shadow_llm_metrics`.
3. Lock the metric name in
   `tests/test_metrics_endpoint.py::test_emits_required_metric_families`
   so a rename here fails CI before it silently empties the
   dashboard panel.
4. Add a Grafana panel to
   `ops/observability/grafana/dashboards/shadow_llm.json` with a
   Langfuse drill-down link.
5. Bump the dashboard `version` field in the JSON.
6. Rebuild + push the `asoe-grafana` image so the change reaches
   Azure (the Compose overlay picks it up on container restart;
   k8s picks it up via ConfigMap reload).

The Prometheus metric name is the contract — don't rename without
updating all five sites in the same PR.
