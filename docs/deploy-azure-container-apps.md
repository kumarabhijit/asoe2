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
PG_ADMIN_PASSWORD='<choose-a-strong-pw>' ./scripts/deploy-azure.sh
```

The script runs a **two-stage deploy** to dodge the placeholder-image
probe-failure trap (the bicep's `/api/v1/health` HTTP probe on port 8000
fails against the `containerapps-helloworld` placeholder, which would
otherwise time out the deployment after ~25 minutes):

1. Registers the resource providers (`Microsoft.App`, `Microsoft.ContainerRegistry`,
   `Microsoft.DBforPostgreSQL`, `Microsoft.Cache`, `Microsoft.OperationalInsights`).
2. Creates the resource group `asoepreprod` if missing.
3. **Cleans up** any prior `Failed` Container App so re-runs start clean.
4. **Stage 1 bicep** (`deployContainerApp=false`): provisions ACR,
   Postgres, Azure Managed Redis, Log Analytics, and the Container Apps
   Managed Environment.
5. **Builds the image** with `az acr build` (cloud builder, no local
   Docker required).
6. **Stage 2 bicep** (`deployContainerApp=true`,
   `containerImage=<just-built-image>`): provisions the Container App
   with the real image — its `/api/v1/health` probe responds correctly
   on the first revision.
7. Prints the FQDN of the API (e.g. `https://asoepreprodapi.<hash>.centralus.azurecontainerapps.io`).

Total time: ~13–18 min on first run (Stage 1 ~10 min for Postgres,
build ~3 min, Stage 2 ~3 min).

> **Why two stages?** The Container App resource refuses to be created
> until its first revision reaches a healthy state. Pre-Stage 2, the
> bicep would default to a generic placeholder image; the real probe
> spec doesn't match what the placeholder serves, so the revision
> never goes healthy and the entire deployment hits its terminal
> "Operation expired" timeout. Stage 1 + image build + Stage 2 avoids
> that entirely.

## Step 2 — Set secrets

The bicep template declares the secret slots but leaves them empty. Until you
populate them, the API will start in fallback mode (or fail to start, if it
cannot reach Postgres / Redis):

```bash
ANTHROPIC_API_KEY=sk-ant-... \
ASOE_JWT_SECRET=auto \
PG_ADMIN_PASSWORD='<the-pw-you-used-in-step-1>' \
    ./scripts/set-secrets.sh
```

`ASOE_JWT_SECRET=auto` generates a fresh 64-byte hex string and prints it once
— save it (e.g. into 1Password) so you can rotate replicas without invalidating
issued tokens.

The script:

- Reads the Postgres FQDN and Redis primary key from Azure.
- Builds `DATABASE_URL` (`postgresql://…?sslmode=require`) and `REDIS_URL`
  (`rediss://…:10000`, no `/db` suffix — Managed Redis exposes a single
  logical DB).
- Calls `az containerapp secret set` for all four secrets.
- Restarts the active revision so the new values are picked up.

## Step 3 — Verify

```bash
RG=asoepreprod
APP=asoepreprodapi

FQDN=$(az containerapp show -g $RG -n $APP \
    --query properties.configuration.ingress.fqdn -o tsv)

curl -fsS https://${FQDN}/api/v1/health | jq .
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

Re-running the script is safe — bicep is idempotent and the image tag defaults
to the current git short SHA, so each commit produces a new revision.

### Inspect / rotate secrets

```bash
# Inspect (only shows names, not values)
az containerapp secret list -g asoepreprod -n asoepreprodapi -o table

# Rotate the Anthropic key only
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
