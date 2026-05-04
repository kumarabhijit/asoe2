# ASOE Azure Container Apps — Infrastructure

This directory holds the bicep template and parameter file used to provision the
pre-prod environment described in `docs/deploy-azure-container-apps.md`.

## Files

| File | Purpose |
| --- | --- |
| `main.bicep` | All Azure resources (ACR, Postgres, Redis, Container App Env + App, Log Analytics, AcrPull RBAC). |
| `parameters.sandbox.json` | Non-secret parameters for the `asoepreprod` environment. |

## Resources provisioned (centralus, RG `asoepreprod`)

> **Region note:** `eastus` rejected the Postgres Flexible Server B1ms
> SKU at the first deploy attempt; `westus2` returned a SKU/capacity
> error on retry. Settled on `centralus`, which has reliable B1ms
> availability and full support for Container Apps + Azure Managed Redis.
>
> **Redis note:** the original `Microsoft.Cache/redis` (Azure Cache for
> Redis) offering is being retired on 2028-09-30 (portal banner). This
> template now uses the replacement service, **Azure Managed Redis**
> (`Microsoft.Cache/redisEnterprise`), at the cheapest tier
> (`Balanced_B0`, ~250 MB). It is built on Redis Enterprise, runs in
> EnterpriseCluster mode (single endpoint, transparent shard routing),
> and is cheaper than legacy Basic C0.

| Resource | Name | SKU | Approx. monthly cost (USD, sandbox) |
| --- | --- | --- | --- |
| Log Analytics workspace | `asoepreprodlogs` | Pay-as-you-go (PerGB2018), 30-day retention | ~$2-5 (low log volume) |
| Container Registry | `asoepreprodacr` | Basic | ~$5 |
| PostgreSQL Flexible Server | `asoepreprodpg` | `Standard_B1ms` Burstable, 32 GB | ~$13 |
| PostgreSQL database | `asoe` (inside above) | n/a | included |
| Azure Managed Redis | `asoepreprodredis` | `Balanced_B0` (~250 MB, Enterprise cluster) | ~$12 |
| Container Apps Environment | `asoepreprodenv` | Consumption profile | ~$0 idle (compute billed per request) |
| Container App | `asoepreprodapi` | 0.5 vCPU / 1.0 GiB, min=1 max=2 | ~$15 with 1 replica always on |
| AcrPull role assignment | system-assigned identity → ACR | n/a | free |

**Total sandbox baseline:** ~$45/month with one replica idle (down from
~$50 with legacy Redis), scales up under load. Container Apps charges per
vCPU-second + GiB-second + request, so quiet periods cost very little.

## What is NOT in bicep (set after deploy)

- Container App secrets (`anthropic-api-key`, `asoe-jwt-secret`,
  `database-url`, `redis-url`, `langfuse-public-key`,
  `langfuse-secret-key`) — declared empty in bicep and populated by
  `scripts/set-secrets.sh`. The LangFuse pair is optional; when unset,
  `observability/langfuse_sink.py` no-ops and stdlib logging stays
  authoritative.
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
- [ ] Upgrade Azure Managed Redis to `Balanced_B3` or `MemoryOptimized_M10` for HA + zone redundancy + larger working set.
- [ ] Move secrets to Azure Key Vault and switch the bicep `secrets:`
      block to `keyVaultUrl` + managed-identity references.
- [ ] Set `ASOE_ENV=production` (locks down sandbox routes + CORS).
- [ ] Tighten CORS allow-origin to the production UI host.
- [ ] Add Azure Front Door + WAF in front of the Container App.
- [ ] Enable diagnostic settings on every resource → Log Analytics.
- [ ] Lock the resource group with a `CanNotDelete` lock.
