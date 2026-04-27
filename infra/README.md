# ASOE Azure Container Apps — Infrastructure

This directory holds the bicep template and parameter file used to provision the
pre-prod environment described in `docs/deploy-azure-container-apps.md`.

## Files

| File | Purpose |
| --- | --- |
| `main.bicep` | All Azure resources (ACR, Postgres, Redis, Container App Env + App, Log Analytics, AcrPull RBAC). |
| `parameters.sandbox.json` | Non-secret parameters for the `asoepreprod` environment. |

## Resources provisioned (westus2, RG `asoepreprod`)

> **Region note:** the original target was `eastus`, but Azure declined
> to provision the Postgres Flexible Server B1ms there (capacity /
> SKU unavailability at the time of deploy). Switched to `westus2`,
> which has full Container Apps + Postgres Flex + Redis support.

| Resource | Name | SKU | Approx. monthly cost (USD, sandbox) |
| --- | --- | --- | --- |
| Log Analytics workspace | `asoepreprodlogs` | Pay-as-you-go (PerGB2018), 30-day retention | ~$2-5 (low log volume) |
| Container Registry | `asoepreprodacr` | Basic | ~$5 |
| PostgreSQL Flexible Server | `asoepreprodpg` | `Standard_B1ms` Burstable, 32 GB | ~$13 |
| PostgreSQL database | `asoe` (inside above) | n/a | included |
| Azure Cache for Redis | `asoepreprodredis` | Basic C0 (250 MB) | ~$16 |
| Container Apps Environment | `asoepreprodenv` | Consumption profile | ~$0 idle (compute billed per request) |
| Container App | `asoepreprodapi` | 0.5 vCPU / 1.0 GiB, min=1 max=2 | ~$15 with 1 replica always on |
| AcrPull role assignment | system-assigned identity → ACR | n/a | free |

**Total sandbox baseline:** ~$50/month with one replica idle, scales up under
load. Container Apps charges per vCPU-second + GiB-second + request, so quiet
periods cost very little.

## What is NOT in bicep (set after deploy)

- Container App secrets (`anthropic-api-key`, `asoe-jwt-secret`,
  `database-url`, `redis-url`) — declared empty in bicep and populated
  by `scripts/set-secrets.sh`.
- Container image tag — bicep deploys a placeholder; the deploy script
  pushes the real image to ACR and updates the revision.
- Postgres firewall rules beyond `AllowAllAzureServices`. For local
  developer access, add a rule with your office IP via `az postgres
  flexible-server firewall-rule create`.

## Production hardening checklist (defer)

When you graduate this environment to production:

- [ ] Move Postgres + Redis behind a VNet private endpoint; remove
      public-network-access on both.
- [ ] Enable Postgres geo-redundant backups + zone-redundant HA.
- [ ] Upgrade Redis to `Standard C1` (HA + replication).
- [ ] Move secrets to Azure Key Vault and switch the bicep `secrets:`
      block to `keyVaultUrl` + managed-identity references.
- [ ] Set `ASOE_ENV=production` (locks down sandbox routes + CORS).
- [ ] Tighten CORS allow-origin to the production UI host.
- [ ] Add Azure Front Door + WAF in front of the Container App.
- [ ] Enable diagnostic settings on every resource → Log Analytics.
- [ ] Lock the resource group with a `CanNotDelete` lock.
