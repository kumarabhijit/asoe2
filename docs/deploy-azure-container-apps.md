# ASOE — Deploy to Azure Container Apps (pre-prod)

End-to-end runbook for deploying the FastAPI service (`api/app.py`) to Azure
Container Apps. The current target is the **`asoepreprod`** environment in
`centralus`, sized for sandbox use. Production hardening checklist lives in
`infra/README.md`.

> **Region note:** `eastus` was the original target but rejected the
> Postgres Flexible Server B1ms SKU at deploy time; `westus2` returned
> the same error on retry. Settled on `centralus`, which has reliable
> B1ms availability and full support for Container Apps + Azure Managed
> Redis. If a previous failed `asoepreprod` resource group exists in
> another region, run `az group delete -n asoepreprod --yes --no-wait`
> first — Azure does not allow moving an existing RG to a new region.

> **Redis note:** `Microsoft.Cache/redis` (Azure Cache for Redis) is
> retiring on 2028-09-30. This template uses **Azure Managed Redis**
> (`Microsoft.Cache/redisEnterprise`) at the cheapest tier
> (`Balanced_B0`, ~250 MB), which is the recommended replacement and is
> cheaper than legacy Basic C0. Connection details: TLS-only, port
> `10000` (not `6380`), single logical database (`default`).

## What gets deployed

```
┌──────────────────────────────────────────────────────────┐
│ Resource group: asoepreprod   (centralus)                │
│                                                          │
│  ┌─────────────────────────┐    ┌─────────────────────┐  │
│  │ Container App:          │◄───┤ ACR: asoepreprodacr │  │
│  │  asoepreprodapi         │    └─────────────────────┘  │
│  │  • 0.5 vCPU / 1 GiB     │                             │
│  │  • min=1 max=2 replicas │    ┌─────────────────────┐  │
│  │  • https ingress :443   │    │ Log Analytics:      │  │
│  │  • sticky sessions (WS) │───►│ asoepreprodlogs     │  │
│  └────────┬────────────────┘    └─────────────────────┘  │
│           │                                              │
│     ┌─────┴───────┐                                      │
│     ▼             ▼                                      │
│  ┌──────────┐   ┌───────────────────┐                    │
│  │ Managed  │   │ PostgreSQL Flex.  │                    │
│  │ Redis    │   │ Standard_B1ms     │                    │
│  │ Bal._B0  │   │ db: asoe          │                    │
│  │ port 10k │   └───────────────────┘                    │
│  └──────────┘                                            │
└──────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Azure subscription**: `f6f24d74-9f1a-4717-94d2-4eef4a617aa0` (Owner or
  Contributor + User Access Administrator).
- **Azure CLI** 2.55+ — install from <https://aka.ms/InstallAzureCLI>.
- **Local clone of this repo** (the deploy script reads `Dockerfile.api`
  and `infra/main.bicep`).
- **A strong Postgres admin password** ready to paste at deploy time.
- **A valid Anthropic API key** (`sk-ant-…`). If you previously shared the
  key in any chat or ticket, **rotate it** at
  <https://console.anthropic.com/settings/keys> first.

## One-time Azure CLI setup

```bash
az login
az account set --subscription f6f24d74-9f1a-4717-94d2-4eef4a617aa0
az extension add --name containerapp --upgrade
```

## Step 1 — Provision infra and deploy the API

From the repo root:

```bash
PG_ADMIN_PASSWORD='<choose-a-strong-pw>' \
ANTHROPIC_API_KEY='sk-ant-<your-key>' \
    ./scripts/deploy-azure.sh
```

`deploy-azure.sh` is the single source of truth for the infra **and** all
four Container App secrets. It runs a **two-stage bicep deploy**:

1. Registers the resource providers (`Microsoft.App`, `Microsoft.ContainerRegistry`,
   `Microsoft.DBforPostgreSQL`, `Microsoft.Cache`, `Microsoft.OperationalInsights`,
   `Microsoft.ManagedIdentity`).
2. Creates the resource group `asoepreprod` if missing.
3. **Cleans up** any prior `Failed` Container App so re-runs start clean.
4. **Stage 1 bicep** (`deployContainerApp=false`): provisions ACR,
   Postgres (with `pgcrypto` + `vector` allow-listed), Azure Managed
   Redis, Log Analytics, the Container Apps Managed Environment, the
   User-Assigned Managed Identity (UAMI), and the AcrPull role binding.
5. **Builds the image** with `az acr build` (cloud builder, no local
   Docker required).
6. **Derives connection strings**: queries Postgres FQDN + Redis hostname
   + Redis primary key from infra, URL-encodes the password and the key,
   builds `DATABASE_URL` and `REDIS_URL`. Preserves `ANTHROPIC_API_KEY`
   and `ASOE_JWT_SECRET` from the existing Container App if it already
   exists (so re-runs don't wipe them); auto-generates a fresh
   `ASOE_JWT_SECRET` on the very first deploy.
7. **Stage 2 bicep** (`deployContainerApp=true`, all four secrets +
   `containerImage=<just-built>` passed as `@secure()` parameters):
   provisions the Container App with the real image and real secrets,
   so `/api/v1/health` + Postgres migrations both succeed on first revision.
8. Prints the FQDN.

Total time: ~13–18 min on first run (Stage 1 ~10 min for Postgres,
build ~1–3 min, Stage 2 ~3–5 min). Subsequent runs are faster because
Stage 1 is mostly idempotent.

### Env vars

| Variable | First deploy | Subsequent deploys |
|---|---|---|
| `PG_ADMIN_PASSWORD` | **required** | **required** (must match Postgres admin password) |
| `ANTHROPIC_API_KEY` | **required** | optional — preserved unless overridden to rotate |
| `ASOE_JWT_SECRET` | optional — auto-generated if unset | optional — preserved unless overridden; pass `auto` to rotate |

> **Why two stages?** The Container App resource refuses to be created
> until its first revision reaches a healthy state. Pre-Stage 2, the
> bicep would default to a generic placeholder image; the real probe
> spec doesn't match what the placeholder serves, so the revision
> never goes healthy and the entire deployment hits its terminal
> "Operation expired" timeout. Stage 1 + image build + Stage 2 avoids
> that entirely. The intermediate "derive connection strings" step
> means re-runs of the script never overwrite the running secrets
> with placeholders.

## Step 2 — Verify

```bash
RG=asoepreprod
APP=asoepreprodapi

FQDN=$(az containerapp show -g $RG -n $APP \
    --query properties.configuration.ingress.fqdn -o tsv)

curl -fsS --max-time 30 "https://${FQDN}/api/v1/health" | jq .
```

Expected response (truncated):

```json
{
  "status": "ok",
  "version": "0.3.2",
  "kill_switch": false,
  "explain_mode": false,
  "allowed_intents": ["…"],
  "lifecycle_states": ["…"]
}
```

Tail logs while you exercise the endpoint:

```bash
az containerapp logs show -g $RG -n $APP --follow
```

## Step 4 — Point the UI at the API

In `asoe-ui` (Vercel project), set the API base URL env var to
`https://<FQDN>` and redeploy.

`ASOE_ENV=sandbox` causes the API's CORS middleware to allow the
`https://asoe-ui.vercel.app` origin (configured in `infra/parameters.sandbox.json`).
If you change the UI host, update `corsAllowedOrigin` in the parameter file
and re-run `scripts/deploy-azure.sh` (or just `az deployment group create`).

## Operations

### Roll a new image after a code change

```bash
PG_ADMIN_PASSWORD=… ./scripts/deploy-azure.sh
```

Re-running is safe — bicep is idempotent, the image tag defaults to the
current git short SHA, and `ANTHROPIC_API_KEY` / `ASOE_JWT_SECRET` are
preserved from the running Container App so secrets do not regress to
placeholders.

### Rotate the Anthropic key

Cheap path (no infra deploy, ~30 s):

```bash
ANTHROPIC_API_KEY='sk-ant-NEW' ./scripts/set-secrets.sh
```

Or via the deploy script (re-runs the full ~5-min bicep+build+bicep
flow but is the only path if you also need an image refresh):

```bash
ANTHROPIC_API_KEY='sk-ant-NEW' PG_ADMIN_PASSWORD=… ./scripts/deploy-azure.sh
```

### Rotate the JWT secret

```bash
ASOE_JWT_SECRET=auto ./scripts/set-secrets.sh
```

This invalidates **all currently-issued JWTs** — every authenticated
client must re-login. The script prints the new value once; save it.

### Inspect secrets

```bash
# Names only (values redacted unless you query individually)
az containerapp secret list -g asoepreprod -n asoepreprodapi -o table

# Direct CLI rotation (alternative to set-secrets.sh)
az containerapp secret set -g asoepreprod -n asoepreprodapi \
    --secrets anthropic-api-key=sk-ant-NEW…

# Roll the active revision so it picks up the new secret
REV=$(az containerapp revision list -g asoepreprod -n asoepreprodapi \
    --query "[?properties.active].name | [0]" -o tsv)
az containerapp revision restart -g asoepreprod -n asoepreprodapi --revision $REV
```

### Rollback to a previous revision

Container Apps keeps revision history; switch traffic with one command:

```bash
az containerapp revision list -g asoepreprod -n asoepreprodapi -o table
az containerapp ingress traffic set -g asoepreprod -n asoepreprodapi \
    --revision-weight <older-revision-name>=100
```

### Scale knobs

Edit `infra/parameters.sandbox.json` (`minReplicas`, `maxReplicas`, `cpu`,
`memory`) and re-run the deploy script. The Container App scale rule is
HTTP-based, target 50 concurrent requests per replica.

### Tear down

```bash
az group delete -n asoepreprod --yes --no-wait
```

This deletes everything provisioned by the bicep template. Postgres backups
are deleted with the server. ACR images are gone too.

## Custom domain (later)

You answered "I don't own asoecore.com" — so the API will be served on the
auto-generated `*.centralus.azurecontainerapps.io` URL. When you do own a
domain, hooking it up takes:

1. `az containerapp hostname add` to bind the hostname.
2. Add the TXT verification + CNAME records at your DNS host.
3. `az containerapp hostname bind` to issue a free managed cert.

That's a 10-minute change; doing it later does not require re-deploying.

## Costs (sandbox, ballpark)

See `infra/README.md` — total ~$50/mo with one replica idle, scales with load.
Container Apps consumption profile bills per vCPU-second, so quiet weekends
are nearly free.
