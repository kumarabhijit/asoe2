# ASOE — Deploy to Azure Container Apps (pre-prod)

End-to-end runbook for deploying the FastAPI service (`api/app.py`) to Azure
Container Apps. The current target is the **`asoepreprod`** environment in
`westus2`, sized for sandbox use. Production hardening checklist lives in
`infra/README.md`.

> **Region note:** `eastus` was the original target, but Azure refused to
> provision Postgres Flexible Server B1ms there (capacity / SKU
> unavailability at the time of the first deploy). All resources moved
> to `westus2`. If the original `eastus` resource group was created and
> needs to be removed, run `az group delete -n asoepreprod --yes
> --no-wait` before re-running the deploy script — Azure does not allow
> moving an existing resource group to a different region.

## What gets deployed

```
┌──────────────────────────────────────────────────────────┐
│ Resource group: asoepreprod   (westus2)                  │
│                                                          │
│  ┌─────────────────────────┐    ┌─────────────────────┐  │
│  │ Container App: asoepre… │◄───┤ ACR: asoepreprodacr │  │
│  │  asoepreprodapi         │    └─────────────────────┘  │
│  │  • 0.5 vCPU / 1 GiB     │                             │
│  │  • min=1 max=2 replicas │    ┌─────────────────────┐  │
│  │  • https ingress :443   │    │ Log Analytics:      │  │
│  │  • sticky sessions (WS) │───►│ asoepreprodlogs     │  │
│  └────────┬────────────────┘    └─────────────────────┘  │
│           │                                              │
│     ┌─────┴─────┐                                        │
│     ▼           ▼                                        │
│  ┌───────┐   ┌───────────────────┐                       │
│  │ Redis │   │ PostgreSQL Flex.  │                       │
│  │ Basic │   │ Standard_B1ms     │                       │
│  │ C0    │   │ db: asoe          │                       │
│  └───────┘   └───────────────────┘                       │
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

The script:

1. Registers the resource providers (`Microsoft.App`, `Microsoft.ContainerRegistry`,
   `Microsoft.DBforPostgreSQL`, `Microsoft.Cache`, `Microsoft.OperationalInsights`).
2. Creates the resource group `asoepreprod` if missing.
3. Runs `az deployment group create` against `infra/main.bicep` — this stands up
   ACR, Postgres, Redis, the Container Apps Environment, and the Container App
   (with empty secrets and a placeholder image).
4. Builds the API image in ACR with `az acr build` (no local Docker required).
5. Updates the Container App revision to the freshly built image.
6. Prints the FQDN of the API (e.g. `https://asoepreprodapi.<hash>.westus2.azurecontainerapps.io`).

Total time: ~10–15 min on first run (Postgres provisioning is the long pole).

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
  (`rediss://…:6380/0`).
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
auto-generated `*.westus2.azurecontainerapps.io` URL. When you do own a
domain, hooking it up takes:

1. `az containerapp hostname add` to bind the hostname.
2. Add the TXT verification + CNAME records at your DNS host.
3. `az containerapp hostname bind` to issue a free managed cert.

That's a 10-minute change; doing it later does not require re-deploying.

## Costs (sandbox, ballpark)

See `infra/README.md` — total ~$50/mo with one replica idle, scales with load.
Container Apps consumption profile bills per vCPU-second, so quiet weekends
are nearly free.
