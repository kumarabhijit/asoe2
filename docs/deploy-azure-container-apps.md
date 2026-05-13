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
| `LANGFUSE_PUBLIC_KEY` | optional — sink stays disabled if unset | optional — preserved unless overridden to rotate |
| `LANGFUSE_SECRET_KEY` | optional — sink stays disabled if unset | optional — preserved unless overridden to rotate |

**LangFuse Cloud (Hobby plan)** is the default observability destination
(`langfuseHost = https://us.cloud.langfuse.com` in
`infra/parameters.sandbox.json`). The bicep template wires
`LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` onto the
API container; `observability/langfuse_sink.py` activates only when all
three are non-empty. To enable forwarding:

1. Create a LangFuse Cloud project at <https://cloud.langfuse.com>
   (US region — matches the configured host) and mint a public/secret
   key pair on the **Settings → API Keys** page.
2. Pass them to the deploy or to `set-secrets.sh`:
   ```bash
   LANGFUSE_PUBLIC_KEY='pk-lf-…' LANGFUSE_SECRET_KEY='sk-lf-…' \
       ./scripts/set-secrets.sh
   ```
3. The active revision restarts automatically; new graph runs forward
   traces + spans + per-LLM-call generation observations to LangFuse.
   Stdlib logging continues unchanged regardless.

Hobby-tier limits (50k observations/month at time of writing — verify
at <https://langfuse.com/pricing>) cover sandbox traffic comfortably
(~10 observations per graph run → ~5,000 runs/month). For a self-hosted
LangFuse stack, point `langfuseHost` at the self-hosted URL and supply
keys minted on that instance.

**Region override.** The default `langfuseHost` is the US Hobby endpoint
(`https://us.cloud.langfuse.com`). To target the EU region or a
self-hosted instance without editing `parameters.sandbox.json`, pass
`LANGFUSE_HOST` as an env var to the deploy script:

```bash
LANGFUSE_HOST='https://cloud.langfuse.com' \
LANGFUSE_PUBLIC_KEY='pk-lf-...' \
LANGFUSE_SECRET_KEY='sk-lf-...' \
ANTHROPIC_API_KEY='sk-ant-...' \
PG_ADMIN_PASSWORD='...' \
    ./scripts/deploy-azure.sh
```

Match the host to the region where you minted the keys — keys minted on
`cloud.langfuse.com` (EU) will not authenticate against
`us.cloud.langfuse.com` and vice versa.

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

You have two options for where the pre-prod UI runs:

**Option A — Vercel (existing).** In `asoe-ui` Vercel project settings, set:

| Var | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://<API_FQDN>` |
| `NEXT_PUBLIC_USE_REAL_API` | `1` |
| `NEXTAUTH_URL` | `https://asoe-ui.vercel.app` (production scope only) |
| `NEXTAUTH_SECRET` | strong 32-byte random |
| `AUTH_TRUST_HOST` | `true` (so per-PR preview hostnames also work) |

Redeploy in Vercel. The API's `CORS_ALLOWED_ORIGIN` already covers
`https://asoe-ui.vercel.app` per `infra/parameters.sandbox.json`. For
Vercel preview URLs (`asoe-ui-git-<branch>-<team>.vercel.app`), set
`corsAllowedOriginRegex` in the parameter file to a pattern that
matches your team's preview URL shape and re-run `scripts/deploy-azure.sh`.

**Option B — Azure Container Apps (recommended for pre-prod).** Run the
deploy script with `DEPLOY_UI=1`. This builds the asoe-ui Next.js
standalone image into ACR, with `NEXT_PUBLIC_API_URL=https://<API_FQDN>`
baked in at build time, and provisions a sister Container App in the
same managed environment as the API:

```bash
DEPLOY_UI=1 ASOE_UI_PATH=../asoe-ui PG_ADMIN_PASSWORD='...' ./scripts/deploy-azure.sh
```

The bicep template computes the UI's deterministic FQDN from
`cae.properties.defaultDomain` and:

- pre-loads it into the API's `CORS_ALLOWED_ORIGINS`, so the API
  accepts the UI without a follow-up redeploy;
- bakes it into the UI's `NEXTAUTH_URL` so NextAuth's callback URLs
  resolve correctly;
- preserves `NEXTAUTH_SECRET` across re-runs (auto-generated on first
  deploy; pass `NEXTAUTH_SECRET=auto` to rotate).

Why pre-prod on Azure? Same audit boundary as the core service,
private networking possible, single Log Analytics workspace, no
cross-vendor data egress for SOX-relevant payloads. Vercel stays
ideal for dev / per-PR previews.

For UI-only redeploys (no infra/API changes), use the fast path:

```bash
./scripts/redeploy-ui.sh
```

## Operations

### Azure CLI cheat sheet

Set these once per shell session and the rest of the commands work as-is:

```bash
RG=asoepreprod
APP=asoepreprodapi
PG=asoepreprodpg
REDIS=asoepreprodredis
ACR=asoepreprodacr
WS=$(az monitor log-analytics workspace show -g $RG -n asoepreprodlogs --query customerId -o tsv)
FQDN=$(az containerapp show -g $RG -n $APP --query properties.configuration.ingress.fqdn -o tsv)
```

| Need | Command |
|---|---|
| API URL | `echo "https://${FQDN}"` |
| Health-check | `curl -fsS --max-time 30 "https://${FQDN}/api/v1/health" \| jq .` |
| Live status | `az containerapp show -g $RG -n $APP --query "properties.{state:provisioningState, runningStatus:runningStatus, fqdn:configuration.ingress.fqdn}" -o json` |
| Active revision summary | `az containerapp revision list -g $RG -n $APP --query "[?properties.active].{name:name, healthState:properties.healthState, runningState:properties.runningState, replicas:properties.replicas}" -o table` |
| Replicas (per-pod state) | `az containerapp replica list -g $RG -n $APP --revision $(az containerapp revision list -g $RG -n $APP --query "[?properties.active].name \| [0]" -o tsv) -o table` |
| Console logs (no streaming) | `az monitor log-analytics query -w $WS --analytics-query "ContainerAppConsoleLogs_CL \| where ContainerAppName_s == 'asoepreprodapi' \| where TimeGenerated > ago(15m) \| order by TimeGenerated asc \| project TimeGenerated, Log_s" -o tsv` |
| System events (image pull, scheduling) | `az monitor log-analytics query -w $WS --analytics-query "ContainerAppSystemLogs_CL \| where ContainerAppName_s == 'asoepreprodapi' \| where TimeGenerated > ago(30m) \| order by TimeGenerated desc \| project TimeGenerated, Reason_s, Log_s" -o tsv` |
| Streaming logs | `az containerapp logs show -g $RG -n $APP --follow` (flaky over corp networks — fall back to the Log Analytics query above) |
| Restart active revision | `az containerapp revision restart -g $RG -n $APP --revision $(az containerapp revision list -g $RG -n $APP --query "[?properties.active].name \| [0]" -o tsv)` |
| Secret names | `az containerapp secret list -g $RG -n $APP -o table` |
| Postgres status | `az postgres flexible-server show -g $RG -n $PG --query "{state:state, version:version, fqdn:fullyQualifiedDomainName, sku:sku.name}" -o json` |
| Redis primary key | `az redisenterprise database list-keys --cluster-name $REDIS -g $RG --query primaryKey -o tsv` |
| ACR image list | `az acr repository show-tags --name $ACR --repository asoe-api --orderby time_desc -o table` |

### End-to-end smoke test

After the API + UI are deployed and you've verified `/api/v1/health` is
green, drive the deterministic pipeline with synthetic events:

```bash
API_URL=https://${FQDN} \
USER_EMAIL=marcus.webb@acme-corp.com \
    ./scripts/smoke-e2e.sh
```

The script logs in, optionally resets the sandbox tenant, then POSTs
every `tests/fixtures/synthetic/<intent>.event.json` to
`/api/v1/exceptions/resolve` and asserts the response carries the
expected intent + recipe and a shadow verdict / final status from
the allowed sets in the matching `<intent>.expected.json`. Coverage
is one fixture per intent (10 of 11 — `MASS_PRICING_ERROR` is
intentionally skipped because it has no recipe and routes to
`FAIL_TO_HUMAN` by design).

What's validated end-to-end:

- `/api/auth/login` round-trip + JWT validity
- Intent classifier (LLM call when `ASOE_LLM_PROVIDER=anthropic`,
  deterministic when `=fallback`)
- Recipe registry routes intent → expected recipe
- Compliance Shadow + executor reach a terminal state in the allowed set
- The exception is persisted (subsequent `GET /api/v1/exceptions/{id}`
  retrieves it; the UI surfaces it via the `/exceptions` page)

Knobs:

| Env var | Default | Purpose |
|---|---|---|
| `API_URL` | live pre-prod FQDN | API base URL |
| `USER_EMAIL` | `jane@acme.com` | Seeded user (admin/manager/analyst) |
| `USER_PASSWORD` | `smoke-e2e` | Any non-empty (V1 stub auth) — shows up in audit logs |
| `RESET_TENANT` | `1` | Reset the in-memory exception store before the run |
| `STOP_ON_FAIL` | `0` | Set to `1` to abort on the first failing fixture |

Each event carries `metadata.synthetic=true` and
`metadata.source="smoke-e2e"` so the audit chain can distinguish smoke
traffic from real ingest. Offline lint of the fixture format runs in
the regular pytest suite (`tests/test_synthetic_fixtures_shape.py`).

### Launch (first deploy)

```bash
az login
az account set --subscription f6f24d74-9f1a-4717-94d2-4eef4a617aa0

PG_ADMIN_PASSWORD='<strong-pw>' \
ANTHROPIC_API_KEY='sk-ant-<your-key>' \
    ./scripts/deploy-azure.sh
```

`ASOE_JWT_SECRET` is auto-generated and printed once. Save it (e.g. into
1Password) — if you ever need to rotate the Container App while keeping
issued JWTs valid, you'll need this exact value. Otherwise the next
auto-generation invalidates all in-flight tokens.

### Tune JWT access-token lifetime per deployment

The default access-token TTL is env-aware:

- `ASOE_ENV=sandbox` → **24h** access, 30d refresh (lets demos / Playwright
  runs survive coffee breaks without silent 401s)
- `ASOE_ENV=production` → **60min** access, 7d refresh (standard
  short-lived access with rotation)

Override per deployment by setting the matching bicep params (or the
underlying env vars directly on the Container App). Operator-friendly
presets:

| Lifetime | `accessTokenTtlSeconds` | Use case |
|---|---|---|
| 15 minutes | `900` | Strict short-lived tokens; tighter security envelope |
| 1 hour | `3600` | Production default — quick rotation with refresh |
| 24 hours | `86400` | Sandbox / demo default — survives idle |

To change for the running app without re-running the full deploy:

```bash
az containerapp update -n asoepreprodapi -g $RG \
    --set-env-vars "ASOE_ACCESS_TOKEN_TTL_SECONDS=3600"
```

The change takes effect on the next revision (Container Apps re-imports
`api.deps` on every restart). Existing tokens keep their original
expiry; only newly issued ones use the new value.

Empty / unset / malformed values fall back to the env-driven default —
the resolver in `api/deps.py::_resolve_token_ttls()` is defensive so
a hand-edit accident can't crash startup.

### Re-deploy (code change, env var change, infra tweak)

```bash
PG_ADMIN_PASSWORD='<same-pw>' ./scripts/deploy-azure.sh
```

Re-running is safe:

- bicep is idempotent.
- The image tag defaults to the current git short SHA, so each commit
  produces a new revision (and thus a rollback target — see below).
- `ANTHROPIC_API_KEY` and `ASOE_JWT_SECRET` are read off the running
  Container App and reused, so secrets do not regress to placeholders.
- `DATABASE_URL` and `REDIS_URL` are re-derived from the live infra,
  which means rotating the Postgres password (next subsection) is
  picked up automatically.

### Roll a new image after a code change

Same command as Re-deploy. The image build step is the slowest part
(~1–3 min); the Container App revision swap is ~30 s after that.

### Rotate the Anthropic key

Cheap path (no infra deploy, ~30 s):

```bash
ANTHROPIC_API_KEY='sk-ant-NEW' ./scripts/set-secrets.sh
```

Or via the deploy script (re-runs the full ~5-min bicep+build+bicep
flow but is the only path if you also need an image refresh):

```bash
ANTHROPIC_API_KEY='sk-ant-NEW' PG_ADMIN_PASSWORD='<same>' ./scripts/deploy-azure.sh
```

### Rotate the JWT secret

```bash
ASOE_JWT_SECRET=auto ./scripts/set-secrets.sh
```

This invalidates **all currently-issued JWTs** — every authenticated
client must re-login. The script prints the new value once; save it.

### Rotate the LangFuse keys

LangFuse keys are paired — `set-secrets.sh` refuses to set just one of
the two, since the sink only activates when both are present.

```bash
LANGFUSE_PUBLIC_KEY='pk-lf-NEW' LANGFUSE_SECRET_KEY='sk-lf-NEW' \
    ./scripts/set-secrets.sh
```

Use the LangFuse Cloud "Rotate keys" workflow on the project's
**Settings → API Keys** page first, then run the command above. Trace
forwarding pauses for a few seconds during the revision restart; stdlib
logging continues uninterrupted.

### Rotate the Postgres admin password

Two-step: change it on the server, then push the new value through the
deploy script so the Container App secret picks it up.

```bash
NEW_PG_PW='<new-strong-password>'

# 1. Update Postgres itself.
az postgres flexible-server update -g $RG -n $PG \
    --admin-password "${NEW_PG_PW}"

# 2. Re-run deploy with the new password — DATABASE_URL is rebuilt and
#    pushed to the Container App as the database-url secret. Existing
#    ANTHROPIC_API_KEY / ASOE_JWT_SECRET are preserved.
PG_ADMIN_PASSWORD="${NEW_PG_PW}" ./scripts/deploy-azure.sh
```

### Rotate the Redis primary key

Azure rotates one key at a time so connections never go cold:

```bash
# 1. Rotate the secondary key first (no callers using it).
az redisenterprise database regenerate-key \
    --cluster-name $REDIS --resource-group $RG -n default \
    --key-type Secondary

# 2. Switch the running app to the new secondary key (treated as the
#    primary by deploy-azure.sh after the next regen).
az redisenterprise database regenerate-key \
    --cluster-name $REDIS --resource-group $RG -n default \
    --key-type Primary

# 3. Re-deploy so REDIS_URL is rebuilt with the new primary.
PG_ADMIN_PASSWORD='<same>' ./scripts/deploy-azure.sh
```

### Inspect secrets

```bash
# Names only (values are not returned by 'list')
az containerapp secret list -g $RG -n $APP -o table

# Direct CLI rotation (alternative to set-secrets.sh — bypasses the
# revision restart, so call it explicitly afterwards)
az containerapp secret set -g $RG -n $APP \
    --secrets anthropic-api-key=sk-ant-NEW…

REV=$(az containerapp revision list -g $RG -n $APP \
    --query "[?properties.active].name | [0]" -o tsv)
az containerapp revision restart -g $RG -n $APP --revision $REV
```

### Connect to Postgres directly (debugging)

The Postgres firewall allows `AllowAllAzureServices` by default and
nothing else. To run psql from your laptop:

```bash
# 1. Add a one-off firewall rule for your current public IP.
MY_IP=$(curl -fsS https://api.ipify.org)
az postgres flexible-server firewall-rule create \
    -g $RG -n $PG --rule-name "tmp-$(whoami)" \
    --start-ip-address $MY_IP --end-ip-address $MY_IP

# 2. Connect.
psql "host=${PG}.postgres.database.azure.com port=5432 dbname=asoe \
      user=asoeadmin password='<your-pg-pw>' sslmode=require"

# 3. Remove the rule when done.
az postgres flexible-server firewall-rule delete \
    -g $RG -n $PG --rule-name "tmp-$(whoami)" --yes
```

### Rollback to a previous revision

Container Apps keeps revision history; switch traffic with one command:

```bash
az containerapp revision list -g $RG -n $APP -o table
az containerapp ingress traffic set -g $RG -n $APP \
    --revision-weight <older-revision-name>=100
```

### Scale knobs

Edit `infra/parameters.sandbox.json` (`minReplicas`, `maxReplicas`, `cpu`,
`memory`) and re-run the deploy script. The Container App scale rule is
HTTP-based, target 50 concurrent requests per replica.

### Tear down

```bash
az group delete -n $RG --yes --no-wait
```

This deletes everything provisioned by the bicep template. Postgres backups
are deleted with the server. ACR images are gone too.

## Troubleshooting

Issues we actually hit during the first deploys, with the exact symptom
and fix. Match the symptom to your case before applying a fix.

### `Operation expired` on the Container App resource

Symptom (in `az deployment group create` output):

```
ContainerAppOperationError: Failed to provision revision for container app
'asoepreprodapi'. Error details: Operation expired.
```

Cause: the bicep created the Container App with the placeholder image
`mcr.microsoft.com/azuredocs/containerapps-helloworld` (port 80, no
`/api/v1/health`), so the probe never goes healthy and ARM hits its
25-min terminal timeout.

Fix: already in place — the deploy script runs a two-stage bicep with
the real image baked in for Stage 2. If you ever see this again:

```bash
az containerapp delete -g $RG -n $APP --yes   # clear the failed app
PG_ADMIN_PASSWORD='<same>' ANTHROPIC_API_KEY='sk-ant-...' ./scripts/deploy-azure.sh
```

### `ACR token exchange endpoint returned error status: 401`

Symptom (in system logs):

```
Failed to construct registry secret for registry 'asoepreprodacr.azurecr.io'.
Error: ACR token exchange endpoint returned error status: 401.
```

Cause: the Container App's identity didn't have AcrPull on the registry
when it tried to pull. This is fixed by using a User-Assigned Managed
Identity created and granted AcrPull in Stage 1, so RBAC has propagated
by the time Stage 2 runs.

Fix: already in place. If a previous failed run left a Container App
with a system-assigned identity around, delete it before retrying:

```bash
az containerapp delete -g $RG -n $APP --yes
PG_ADMIN_PASSWORD='<same>' ANTHROPIC_API_KEY='sk-ant-...' ./scripts/deploy-azure.sh
```

### `could not translate host name "<garbage>@…postgres.database.azure.com"`

Symptom (in console logs):

```
psycopg2.OperationalError: could not translate host name
"<some-fragment>@asoepreprodpg.postgres.database.azure.com" to address
```

Cause: the Postgres password contains `@` (or another reserved URL char)
and was spliced into `DATABASE_URL` without URL-encoding, so psycopg2
split at the wrong `@`.

Fix: already in place — the deploy script URL-encodes the password and
the Redis key before assembling the connection strings. If you see this
again, just re-run:

```bash
PG_ADMIN_PASSWORD='<same>' ./scripts/deploy-azure.sh
```

### `extension "pgcrypto" is not allow-listed for users`

Symptom (in console logs):

```
psycopg2.errors.FeatureNotSupported: extension "pgcrypto" is not
allow-listed for users in Azure Database for PostgreSQL
```

Cause: Azure Postgres Flexible Server gates `CREATE EXTENSION` behind
the `azure.extensions` server parameter, which defaults to empty. The
asoe migrations need `pgcrypto` and `vector`.

Fix: already in place — the bicep sets `azure.extensions=PGCRYPTO,VECTOR`
on the server. If a previously-deployed server didn't have this,
re-running the deploy applies the parameter (no server restart needed):

```bash
PG_ADMIN_PASSWORD='<same>' ./scripts/deploy-azure.sh
```

### 504 from the API

Symptom: `curl https://${FQDN}/api/v1/health` returns HTTP 504 after a
long wait. Container App ingress thinks the backend is healthy but the
request didn't get a reply in time.

Most common cause: the app crashed at startup (often during the
Postgres migration). Check console logs:

```bash
az monitor log-analytics query -w $WS --analytics-query "
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'asoepreprodapi'
| where TimeGenerated > ago(15m)
| order by TimeGenerated asc
| project TimeGenerated, Log_s
" -o tsv
```

The traceback will point at the actual issue — usually one of the
above patterns or a missing-secret problem.

### `invalid dsn: missing "=" after "placeholder-set-via-set-secrets-sh"`

Cause: the Container App is running with placeholder secrets because
`deploy-azure.sh` was run before `df825fa` (when secrets had to be set
separately by `set-secrets.sh`).

Fix: re-deploy with the current scripts — they own the secrets end-to-end:

```bash
git pull
PG_ADMIN_PASSWORD='<same>' ANTHROPIC_API_KEY='sk-ant-...' ./scripts/deploy-azure.sh
```

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

## Automated CI deploy (ADR-041 P6)

`.github/workflows/deploy-azure.yml` runs `scripts/deploy-azure.sh`
automatically on every push to `main` after the `tests` workflow is
green for the same SHA. Health-checks the new revision and rolls back
via `az containerapp revision deactivate` if `/api/v1/health` doesn't
return 200 within 60 seconds.

### Required GitHub configuration

**Repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Source |
|---|---|
| `AZURE_CLIENT_ID` | The federated identity's app registration client ID. |
| `AZURE_TENANT_ID` | The Azure AD tenant ID. |
| `AZURE_SUBSCRIPTION_ID` | The subscription owning `asoepreprod`. |
| `PG_ADMIN_PASSWORD` | Postgres admin password (same value used in manual deploys). |
| `ANTHROPIC_API_KEY` | Required on the first automated deploy; preserved on re-runs. |
| `ASOE_JWT_SECRET` | Set to `auto` for first deploy; preserved thereafter. |

**Repository variables** (Settings → Secrets and variables → Variables):

| Variable | Default value | Notes |
|---|---|---|
| `AZURE_RESOURCE_GROUP` | `asoepreprod` | Resource group name. |
| `AZURE_LOCATION` | `centralus` | Region. |
| `AZURE_ACR_NAME` | _your ACR name_ | Set to the prefix only, no `.azurecr.io`. |
| `AZURE_APP_NAME` | _your API app name_ | Container App resource name. |

### Federated credential setup (one-time)

Azure → App registrations → your-app → Certificates & secrets →
Federated credentials → Add credential.

  * **Federated credential scenario:** GitHub Actions deploying Azure resources.
  * **Organization:** `kumarabhijit` (or your org).
  * **Repository:** `asoe2`.
  * **Entity type:** Branch.
  * **Branch name:** `main`.

Repeat with **Entity type: Environment** + **Environment name: production**
to allow `workflow_dispatch` runs from non-main refs.

### Manual override

The workflow also accepts `workflow_dispatch` with an optional `ref`
input and a `deploy_ui` toggle. Use this to re-deploy a specific SHA
without pushing a no-op commit, or to test the workflow itself.

### Rollback semantics

ACA keeps the previous revision running until the new one is healthy.
If the health-check step times out (12 × 5s = 60s), the workflow runs
`az containerapp revision deactivate` on the new revision — ACA
immediately resumes routing 100% traffic to the previous revision.
The workflow exits non-zero so the run is marked failed in the UI;
the deploy summary records both the failed revision name and the
active (rolled-back) revision name for forensics.

### What's not yet automated

  * **Bicep template changes** still require a manual review-and-deploy
    cycle (the workflow rebuilds the image and re-runs the same script,
    but bicep diffs that change infra topology should be eyeballed
    before they ship — see `infra/README.md`).
  * **asoe-ui deploy** stays on Vercel (preview-per-PR + auto-promote
    on merge) — two-cloud is intentional. The DevOps panel reviewed
    moving asoe-ui to Azure and rejected it (Vercel's preview workflow
    is genuinely better than ASA for this surface).
