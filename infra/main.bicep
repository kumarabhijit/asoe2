// main.bicep ─ ASOE pre-prod infrastructure for Azure Container Apps
//
// Deploys:
//   • Log Analytics workspace          (logs + KQL)
//   • Azure Container Registry (Basic) (image hosting)
//   • Azure Database for PostgreSQL    (Flexible Server, B1ms)
//   • Azure Managed Redis              (Balanced_B0, Enterprise SKU,
//                                       replaces retiring Microsoft.Cache/redis)
//   • Container Apps managed environment
//   • Container App (asoe API)         (system-assigned identity, ACR pull)
//
// Two-stage deploy (driven by `deployContainerApp` parameter):
//   Stage 1 (deployContainerApp=false): provisions everything EXCEPT the
//     Container App. Critically, this includes the User-Assigned Managed
//     Identity (UAMI) and its AcrPull role binding on ACR — so by the
//     time Stage 2 runs, ACR token exchange for the UAMI works on first
//     attempt. (System-assigned identities cause a deterministic 401 race
//     because the role binding lags the Container App creation.)
//   Stage 2 (deployContainerApp=true,  containerImage=<real-image>):
//     creates the Container App, configured to use the pre-existing UAMI
//     for both pod identity and ACR pull. With the real image, the
//     /api/v1/health probe passes on the first revision.
//
// scripts/deploy-azure.sh runs both stages back-to-back.
//
// Secrets are written from `@secure()` parameters with placeholder defaults
// — scripts/set-secrets.sh overwrites them post-deploy with real values.
//
// Parameter file: parameters.sandbox.json
// Deploy:         scripts/deploy-azure.sh

targetScope = 'resourceGroup'

// ───────────────────────────────────────────────────────────────── Parameters

@description('Region for all resources. centralus per pre-prod plan (Postgres Flexible B1ms was unavailable in eastus and westus2 returned a SKU/capacity error during initial provision; centralus has reliable B1ms availability).')
param location string = resourceGroup().location

@description('Naming prefix used for all resources (alphanumeric, no dashes).')
@minLength(3)
@maxLength(20)
param namePrefix string = 'asoepreprod'

@description('ASOE_ENV value injected into the API container. sandbox toggles CORS allowlist + sandbox-only routes; production locks them down.')
@allowed([
  'sandbox'
  'production'
])
param asoeEnv string = 'sandbox'

@description('Single CORS allow-origin (legacy). Kept so existing parameter files keep working; prefer corsAllowedOriginsCsv for multi-origin (pre-prod UI on Azure + Vercel prod + custom domain).')
param corsAllowedOrigin string = 'https://asoe-ui.vercel.app'

@description('Comma-separated CORS allow-origins. Use this when more than one origin must be allowed (e.g. the Azure-hosted pre-prod UI FQDN AND the Vercel production URL). Empty string disables.')
param corsAllowedOriginsCsv string = ''

@description('Optional regex applied to the request Origin (allow_origin_regex). Use to match Vercel preview URLs like https://asoe-ui-git-<branch>-<team>.vercel.app without listing each. Empty string disables.')
param corsAllowedOriginRegex string = ''

@description('JWT access-token lifetime in seconds. Empty string lets api/deps.py pick the env-driven default (sandbox=86400 / production=3600). Set to e.g. 900 for 15min, 3600 for 1h, 86400 for 24h to override.')
param accessTokenTtlSeconds string = ''

@description('JWT refresh-token lifetime in seconds. Empty string lets api/deps.py pick the env-driven default (sandbox=2592000 / production=604800). Set to override per deployment.')
param refreshTokenTtlSeconds string = ''

@description('LLM provider routing. fallback = deterministic only, no outbound LLM traffic.')
@allowed([
  'fallback'
  'anthropic'
  'openai'
  'azure_openai'
  'ollama'
  'huggingface'
])
param llmProvider string = 'anthropic'

@description('PostgreSQL administrator login.')
param pgAdminUser string = 'asoeadmin'

@description('PostgreSQL administrator password. Pass via --parameters at deploy time, do NOT commit.')
@secure()
param pgAdminPassword string

@description('Anthropic API key. Placeholder until set-secrets.sh is run.')
@secure()
param anthropicApiKey string = 'placeholder-set-via-set-secrets-sh'

@description('Precedents embedding provider (sign-off 2026-06-10). Empty (default) keeps semantic matching OFF — precedents fall back to the deterministic correlate match. Allowed: \'\' | openai.')
@allowed([
  ''
  'openai'
])
param embeddingProvider string = ''

@description('Embedding model id. Empty defers to the code default (text-embedding-3-small, 1536 dims — matches the V001 pgvector column).')
param embeddingModel string = ''

@description('OpenAI API key — required only when embeddingProvider=openai (or llmProvider=openai). Placeholder otherwise.')
@secure()
param openaiApiKey string = 'placeholder-set-via-set-secrets-sh'

@description('JWT secret for ASOE auth (HS256, seed mode). Placeholder until set-secrets.sh is run. PARITY-4: rotated independently of the attachment signing key.')
@secure()
param asoeJwtSecret string = 'placeholder-set-via-set-secrets-sh'

@description('Attachment capability-token signing key (api/attachment_read_token.py). PARITY-4: independent of ASOE_JWT_SECRET so rotating attachment-URL signing does NOT invalidate auth tokens. Placeholder until set-secrets.sh is run.')
@secure()
param asoeAttachmentSigningKey string = 'placeholder-set-via-set-secrets-sh'

@description('Attachment signing key rotation overlap. When operator rotates ASOE_ATTACHMENT_SIGNING_KEY, the old value is moved here for the overlap window so tokens minted just before rotation still verify until they expire. Empty disables.')
@secure()
param asoeAttachmentSigningKeySecondary string = ''

@description('PostgreSQL connection string. Placeholder until set-secrets.sh is run.')
@secure()
param databaseUrl string = 'placeholder-set-via-set-secrets-sh'

@description('Redis connection string. Placeholder until set-secrets.sh is run.')
@secure()
param redisUrl string = 'placeholder-set-via-set-secrets-sh'

@description('LangFuse Cloud (or self-hosted) host URL — e.g. https://us.cloud.langfuse.com (US region) or https://cloud.langfuse.com (EU region). Empty string disables LangFuse forwarding (the sink no-ops, stdlib logging continues unchanged).')
param langfuseHost string = 'https://us.cloud.langfuse.com'

@description('LangFuse project public key (pk-lf-...). Placeholder until set-secrets.sh is run; empty value keeps the sink disabled.')
@secure()
param langfusePublicKey string = 'placeholder-set-via-set-secrets-sh'

@description('LangFuse project secret key (sk-lf-...). Placeholder until set-secrets.sh is run; empty value keeps the sink disabled.')
@secure()
param langfuseSecretKey string = 'placeholder-set-via-set-secrets-sh'

@description('Container image reference (set by deploy script after ACR build, e.g. asoepreprodacr.azurecr.io/asoe-api:GIT_SHA).')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('UI container image reference (set by deploy script after ACR build, e.g. asoepreprodacr.azurecr.io/asoe-ui:GIT_SHA). Only consulted when deployUiContainerApp=true.')
param containerImageUi string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('NextAuth signing secret (server-side; rotates the issued NextAuth session cookies). Placeholder until the deploy script generates / preserves a value.')
@secure()
param nextAuthSecret string = 'placeholder-set-by-deploy-script'

@description('Min replicas for the API container app.')
@minValue(0)
@maxValue(10)
param minReplicas int = 1

@description('Max replicas for the API container app.')
@minValue(1)
@maxValue(20)
param maxReplicas int = 2

@description('vCPU per replica.')
param cpu string = '0.5'

@description('Memory per replica (must pair with cpu per Container Apps SKU table; 0.5 vCPU → 1.0Gi).')
param memory string = '1.0Gi'

@description('Azure Managed Redis SKU. Balanced_B0 is the cheapest (~250MB, eviction-only). Bump to Balanced_B1 (~1GB) if B0 is unavailable in the chosen region.')
@allowed([
  'Balanced_B0'
  'Balanced_B1'
  'Balanced_B3'
  'MemoryOptimized_M10'
])
param redisSku string = 'Balanced_B0'

@description('Two-stage deploy gate. Stage 1 (false) provisions ACR / Postgres / Redis / Log Analytics / Managed Env / UAMI / AcrPull role binding so `az acr build` has somewhere to push and RBAC is propagated by the time Stage 2 runs. Stage 2 (true) provisions the Container App itself with the real image. The deploy script flips this between calls.')
param deployContainerApp bool = false

@description('Independent gate for the UI Container App. The deploy script flips this true on its third stage (after the UI image has been built into ACR with NEXT_PUBLIC_API_URL baked in). Kept independent of deployContainerApp so the API and UI can be deployed/redeployed separately without dragging the other along.')
param deployUiContainerApp bool = false

@description('Gate for the ADR-039 §7.3 observability stack — Prometheus + Grafana Container Apps that scrape /api/v1/metrics on the asoe-api app. Kept independent so observability can roll out (or back) without dragging the API. Set true once both images are built into ACR via `docker build -f Dockerfile.prometheus|grafana . && az acr build ...`.')
param deployObservability bool = false

@description('PARITY-8 — gate for the retention-sweeper Container Apps Job (api/retention_sweeper.py). Cron-scheduled bulk-delete job that wipes attachments past their per-tenant retention TTL. Kept off by default; turning it on AND setting RETENTION_SWEEPER_ENABLED=true on the job is the two-step opt-in the Compliance review mandated. Image reuses containerImage (the same ASOE Python container).')
param deployRetentionSweeperJob bool = false

@description('PARITY-8 — cron expression for the retention sweeper. Default 02:00 UTC daily; tighten/loosen per operational cadence. Format is standard 5-field cron (minute hour day month weekday).')
param retentionSweeperCron string = '0 2 * * *'

@description('PARITY-7 followup — gate for the drift-alert forwarder Container Apps Job (scripts/run_drift_forwarder.py). Runs api.observability.drift_alert_integration.evaluate_all_extraction_drift() on a cron schedule and emits a structured extraction-drift log event per detected drift so App Insights / Log Analytics has data for the Azure Monitor alert. Image reuses containerImage. Default off.')
param deployDriftAlertJob bool = false

@description('PARITY-7 — cron expression for the drift-alert forwarder. Default 06:00 UTC daily; the evaluator splits 50 recent vs 200 baseline samples so a daily run is appropriate for the 7-day rolling window in the parity plan.')
param driftAlertCron string = '0 6 * * *'

@description('Prometheus container image (built from Dockerfile.prometheus; bakes in ops/observability/prometheus.yml). Placeholder until set-secrets.sh / deploy pipeline overrides.')
param prometheusImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Grafana container image (built from Dockerfile.grafana; bakes in datasource + dashboard provisioning). Placeholder until set-secrets.sh / deploy pipeline overrides.')
param grafanaImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Grafana initial admin password. Override at deploy time via --parameters; never commit. Empty string keeps the default `admin` which is acceptable only for sandbox.')
@secure()
param grafanaAdminPassword string = ''

@description('LangFuse base URL plumbed into Grafana so the dashboard panels can deep-link from "disagreement spike at 14:32" → individual traces in one click. Falls back to the langfuseHost parameter when empty.')
param grafanaLangfuseBaseUrl string = ''

@description('LangFuse project slug plumbed into the same Grafana deep-links.')
param grafanaLangfuseProject string = 'asoe'

// ───────────────────────────────────────────────────────────── Derived names

var logAnalyticsName = '${namePrefix}logs'
var acrName          = '${namePrefix}acr'
var pgServerName     = '${namePrefix}pg'
var pgDatabaseName   = 'asoe'
var redisName        = '${namePrefix}redis'
var caeName          = '${namePrefix}env'
var appName          = '${namePrefix}api'
var uiAppName        = '${namePrefix}ui'
var uamiName         = '${namePrefix}identity'
var prometheusAppName = '${namePrefix}prom'
var grafanaAppName    = '${namePrefix}graf'
// PARITY-4 — Key Vault. Globally unique (Azure-wide) so we anchor on
// the namePrefix; the 24-char vault-name cap is the binding constraint.
var keyVaultName     = take('${namePrefix}kv', 24)

var commonTags = {
  project: 'asoe'
  env:     asoeEnv
  managed: 'bicep'
}

// ─────────────────────────────────────────────── User-Assigned Managed Identity
//
// Created in Stage 1 so its AcrPull role assignment (below) has time to
// propagate before Stage 2 stands up the Container App. Using a
// system-assigned identity instead causes a deterministic race: the
// Container App resource is created → it tries to pull from ACR → ACR
// returns 401 → role binding propagates a few seconds later but the
// app has already given up.

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
  tags: commonTags
}

// AcrPull on the UAMI for ACR. Always created (independent of
// deployContainerApp) so the role exists by the time Stage 2 runs.
// roleDefinitionId for AcrPull = 7f951dda-4ed3-4680-a7ca-43fe172d538d.
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, uami.id, 'AcrPull')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

// ─────────────────────────────────────────────────────────── Log Analytics

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: union(commonTags, {
    purpose: 'asoe-preprod-monitoring' // PARITY-5 Decision Q5 — dedicated workspace.
  })
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    // PARITY-5 — 30d Log Analytics retention per Decision Q5.
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// PARITY-5 — Application Insights component pointed at the dedicated
// Log Analytics workspace above. App Insights retention is 90 days
// (Decision Q5), longer than the Log Analytics 30d so audit-relevant
// traces survive a full quarter for SOX evidence requests.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}appinsights'
  location: location
  tags: union(commonTags, {
    purpose: 'asoe-preprod-monitoring'
  })
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    RetentionInDays: 90
    IngestionMode: 'LogAnalytics'
    DisableIpMasking: false
  }
}

// ───────────────────────────────────────────────── Azure Container Registry

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: commonTags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    anonymousPullEnabled: false
  }
}

// ─────────────────────────────────────────── PostgreSQL Flexible Server

resource pgServer 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: pgServerName
  location: location
  tags: commonTags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: pgAdminUser
    administratorLoginPassword: pgAdminPassword
    storage: {
      storageSizeGB: 32
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

// Allow all Azure services (Container Apps egress IPs are not stable).
// For production, replace with VNet integration + private endpoint.
//
// ── PARITY-0 Phase 0a — VNet + private-Postgres upgrade path (commented) ──
// The Azure/SRE review of docs/plans/azure-preprod-parity-plan.md flagged
// the `0.0.0.0/0` firewall as acceptable for sandbox but NOT for any real-
// tenant traffic. The upgrade path is:
//
//   1. Add a VNet + subnet for the Container App managed env:
//        resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
//          name: '${namePrefix}vnet'
//          location: location
//          properties: {
//            addressSpace: { addressPrefixes: [ '10.10.0.0/16' ] }
//            subnets: [
//              { name: 'snet-cae', properties: { addressPrefix: '10.10.1.0/23' } }
//              { name: 'snet-pe',  properties: {
//                  addressPrefix: '10.10.4.0/24'
//                  privateEndpointNetworkPolicies: 'Disabled'
//              }}
//            ]
//          }
//        }
//
//   2. Bind the Managed Environment to the snet-cae subnet
//      (vnetConfiguration.infrastructureSubnetId).
//
//   3. Set Postgres `publicNetworkAccess: 'Disabled'` and add a
//      private endpoint on snet-pe:
//        resource pgPe 'Microsoft.Network/privateEndpoints@2024-01-01' = {
//          name: '${namePrefix}-pg-pe'
//          location: location
//          properties: {
//            subnet: { id: '${vnet.id}/subnets/snet-pe' }
//            privateLinkServiceConnections: [{
//              name: 'pg'
//              properties: {
//                privateLinkServiceId: pgServer.id
//                groupIds: [ 'postgresqlServer' ]
//              }
//            }]
//          }
//        }
//
//   4. Delete this firewall rule. Postgres becomes reachable only via the
//      VNet + private DNS zone.
//
// Sequenced for Phase 2 (Azure Blob) and Phase 6 (real connectors): both
// inherit the VNet automatically once it lands. Tracked as PARITY-0
// follow-up; Phase 0a ships against the public-endpoint path on synthetic
// data only.
resource pgFwAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: pgServer
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ── PARITY-0 Phase 0a — Attachment Blob storage (Phase 2 implementation) ──
// Decision Q6+Q7 (parity plan): preprod single-region (location param
// above), Standard_LRS replication. Phase 2 lands the real driver
// (gateways/attachment_store.py::_azure_blob_store) + grants the
// Container App's managed identity `Storage Blob Data Contributor`.
//
// Commented until Phase 2 ships so the current bicep still compiles
// without azure-storage-blob in the runtime image.
//
// param storageAccountName string = '${namePrefix}attachments'
// param storageContainerName string = 'attachments'
//
// resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
//   name: storageAccountName
//   location: location
//   sku: { name: 'Standard_LRS' }   // Decision Q7 — preprod; GA upgrades to GRS
//   kind: 'StorageV2'
//   properties: {
//     accessTier: 'Hot'
//     allowBlobPublicAccess: false   // private only
//     minimumTlsVersion: 'TLS1_2'
//     supportsHttpsTrafficOnly: true
//     publicNetworkAccess: 'Disabled' // VNet + private endpoint per above
//     // Compliance review: soft-delete + versioning mandatory before Phase 2 ships.
//   }
// }
//
// resource storageBlob 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
//   parent: storage
//   name: 'default'
//   properties: {
//     deleteRetentionPolicy: { enabled: true, days: 30 }
//     containerDeleteRetentionPolicy: { enabled: true, days: 30 }
//     isVersioningEnabled: true
//   }
// }
//
// resource storageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
//   parent: storageBlob
//   name: storageContainerName
//   properties: { publicAccess: 'None' }
// }
//
// // Grant the Container App's managed identity Blob Data Contributor.
// resource storageRbac 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
//   name: guid(storage.id, uami.id, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
//   scope: storage
//   properties: {
//     roleDefinitionId: subscriptionResourceId(
//       'Microsoft.Authorization/roleDefinitions',
//       'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // Storage Blob Data Contributor
//     )
//     principalId: uami.properties.principalId
//     principalType: 'ServicePrincipal'
//   }
// }

resource pgDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: pgServer
  name: pgDatabaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Allow-list extensions used by the asoe migrations:
//   pgcrypto — gen_random_uuid() (V001 schema) + audit hash chain (V003)
//   vector   — pgvector for V2 embedding-search readiness (V001 schema)
//
// Azure Postgres Flexible Server gates `CREATE EXTENSION` behind this
// server parameter; without the allow-list the migrations fail with
// `FeatureNotSupported: extension "X" is not allow-listed`. The
// parameter is dynamic — no server restart required.
resource pgExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-12-01-preview' = {
  parent: pgServer
  name: 'azure.extensions'
  properties: {
    value: 'PGCRYPTO,VECTOR'
    source: 'user-override'
  }
}

// PARITY-6 (Integration review) — Azure-native PgBouncer.
//
// MUST land BEFORE the Phase 6.3 SAP sub-phase ships — real SAP
// fan-out will exhaust the default per-connection pool. Azure
// Database for PostgreSQL Flexible Server bundles PgBouncer as a
// server-parameter feature; enable it and switch the application's
// DATABASE_URL to point at port 6432 (PgBouncer) instead of the
// default 5432 (Postgres direct).
//
// Transaction mode matches the LangGraph-async + per-recipe-request
// pattern: short-lived connections, no prepared-statement caches
// across calls. Session mode would defeat the pooling for our
// workload.

resource pgPgBouncerEnabled 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-12-01-preview' = {
  parent: pgServer
  name: 'pgbouncer.enabled'
  properties: {
    value: 'true'
    source: 'user-override'
  }
}

resource pgPgBouncerMode 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-12-01-preview' = {
  parent: pgServer
  name: 'pgbouncer.pool_mode'
  properties: {
    value: 'transaction'
    source: 'user-override'
  }
  dependsOn: [
    pgPgBouncerEnabled
  ]
}

resource pgPgBouncerDefaultPoolSize 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-12-01-preview' = {
  parent: pgServer
  name: 'pgbouncer.default_pool_size'
  properties: {
    // Conservative default; raise after Phase 6.3 baseline metrics
    // show real fan-out demand. The B1ms server-side max_connections
    // is the hard ceiling; PgBouncer multiplexes below it.
    value: '50'
    source: 'user-override'
  }
  dependsOn: [
    pgPgBouncerEnabled
  ]
}

// ──────────────────────────────────────────────────── Azure Managed Redis
//
// Replaces the legacy 'Microsoft.Cache/redis' offering (retiring 2028-09-30
// per the portal banner). Managed Redis is GA, runs on Redis Enterprise,
// and the smallest tier (Balanced_B0, ~250 MB) is cheaper than legacy
// Basic C0. EnterpriseCluster mode exposes a single endpoint that handles
// shard routing internally — non-cluster-aware redis-py clients connect
// transparently for standard key-value and pub/sub ops.
//
// SKU note: if Balanced_B0 is rejected at deploy time (regional
// availability still rolling out in some regions), fall back to
// Balanced_B1 (~1 GB) by editing the redisSku param.

resource redisEnterprise 'Microsoft.Cache/redisEnterprise@2024-10-01' = {
  name: redisName
  location: location
  tags: commonTags
  sku: {
    name: redisSku
  }
}

resource redisDatabase 'Microsoft.Cache/redisEnterprise/databases@2024-10-01' = {
  parent: redisEnterprise
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: 10000
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'AllKeysLRU'
    persistence: {
      aofEnabled: false
      rdbEnabled: false
    }
  }
}

// ────────────────────────────────────────────────────────── Azure Key Vault
//
// PARITY-4 (per Decision Q4 + Security/Compliance review):
//   * RBAC mode — no access policies; least-privilege via role
//     assignments only.
//   * Soft-delete + purge protection — 90-day retention so a
//     mis-clicked delete cannot stem the audit trail or strand a
//     rotating key.
//   * Secrets land in Key Vault; Container App pulls via
//     secretref → Key Vault reference (the secret URI replaces the
//     literal value at deploy time).
//
// First-deploy bootstrap: the secrets are still written to the
// Container App as literal values from the `@secure()` bicep params
// (so a fresh deploy stands up without a manual Key Vault round trip);
// the GA-readiness migration switches the Container App secret
// `keyVaultUrl` so the literal values are dropped.
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: commonTags
  properties: {
    enabledForDeployment: false
    enabledForTemplateDeployment: false
    enabledForDiskEncryption: false
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    publicNetworkAccess: 'Enabled' // first-deploy convenience; GA: lock down to VNet.
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// Grant the Container App's UAMI the Key Vault Secrets User role so
// the Container App can resolve `keyVaultUrl`-style secret refs once
// they replace the literal `value:` entries above.
resource kvSecretsUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  // Role definition id for "Key Vault Secrets User"
  // (4633458b-17de-408a-b874-0445c86b69e6).
  name: guid(keyVault.id, uami.id, '4633458b-17de-408a-b874-0445c86b69e6')
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

// ───────────────────────────────────────── Container Apps Managed Environment

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: caeName
  location: location
  tags: commonTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// ─────────────────────────────────────────── Sister-app FQDNs (deterministic)
//
// Container Apps in the same managed environment share a defaultDomain
// (e.g. `orangerock-0b3a1691.centralus.azurecontainerapps.io`). That lets
// us derive both apps' public FQDNs from cae.properties.defaultDomain and
// thread them through into:
//   * the API's CORS_ALLOWED_ORIGINS (so the browser at the UI FQDN is
//     allowed before the UI Container App resource is even created), and
//   * the UI's NEXTAUTH_URL (so NextAuth knows its canonical origin
//     without us having to two-pass the deploy).
//
// `cae.properties.defaultDomain` is read at deployment time; bicep
// resolves the dependency on `cae`.

var uiFqdn  = '${uiAppName}.${cae.properties.defaultDomain}'

// Compose the effective CORS allowlist the API will receive. The
// caller may pass `corsAllowedOriginsCsv` for additional origins
// (e.g. a custom domain); we always tack the Azure-hosted UI FQDN
// on so the deployed UI talks to the API on day one without a
// follow-up redeploy.
var defaultUiOrigin     = 'https://${uiFqdn}'
var corsAllowedFinalCsv = empty(corsAllowedOriginsCsv) ? defaultUiOrigin : '${corsAllowedOriginsCsv},${defaultUiOrigin}'

// ───────────────────────────────────────────── Container App (ASOE API)

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployContainerApp) {
  name: appName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // Session affinity is required so a WebSocket client (/api/v1/ws)
        // sticks to the replica that owns its connection state.
        stickySessions: {
          affinity: 'sticky'
        }
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          // UAMI created in Stage 1 with AcrPull role pre-granted, so
          // the registry token exchange succeeds on first attempt.
          identity: uami.id
        }
      ]
      // Secrets populated from parameters (defaults are placeholders; use set-secrets.sh
      // to replace with actual values after the first deploy).
      secrets: [
        {
          name: 'anthropic-api-key'
          value: anthropicApiKey
        }
        {
          name: 'openai-api-key'
          value: openaiApiKey
        }
        {
          name: 'asoe-jwt-secret'
          value: asoeJwtSecret
        }
        // PARITY-4 — separate signing key for attachment capability
        // tokens (api/attachment_read_token.py). Independently
        // rotatable from asoe-jwt-secret; see docs/ops/secrets-rotation.md.
        {
          name: 'asoe-attachment-signing-key'
          value: asoeAttachmentSigningKey
        }
        {
          name: 'asoe-attachment-signing-key-secondary'
          value: asoeAttachmentSigningKeySecondary
        }
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'redis-url'
          value: redisUrl
        }
        {
          name: 'langfuse-public-key'
          value: langfusePublicKey
        }
        {
          name: 'langfuse-secret-key'
          value: langfuseSecretKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'asoe-api'
          image: containerImage
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            { name: 'ASOE_ENV',           value: asoeEnv }
            { name: 'ASOE_LLM_PROVIDER',  value: llmProvider }
            // Precedents embedding provider (sign-off 2026-06-10).
            // Empty = semantic OFF (correlate fallback) — parity with
            // local/.env.example and the Vercel mock (correlate-only).
            { name: 'ASOE_EMBEDDING_PROVIDER', value: embeddingProvider }
            { name: 'ASOE_EMBEDDING_MODEL',    value: embeddingModel }
            { name: 'OPENAI_API_KEY',          secretRef: 'openai-api-key' }
            { name: 'ASOE_KILL_SWITCH',   value: '0' }
            { name: 'ASOE_EXPLAIN_MODE',  value: '0' }
            { name: 'CORS_ALLOWED_ORIGIN',        value: corsAllowedOrigin }
            { name: 'CORS_ALLOWED_ORIGINS',       value: corsAllowedFinalCsv }
            { name: 'CORS_ALLOWED_ORIGIN_REGEX',  value: corsAllowedOriginRegex }
            // Empty string defers to api/deps.py:_resolve_token_ttls()
            // which picks the env-driven default (sandbox=24h /
            // production=60min). Set the bicep param to a positive
            // integer string (e.g. '900' for 15min, '3600' for 1h,
            // '86400' for 24h) to override per deployment without a
            // code change.
            { name: 'ASOE_ACCESS_TOKEN_TTL_SECONDS',  value: accessTokenTtlSeconds }
            { name: 'ASOE_REFRESH_TOKEN_TTL_SECONDS', value: refreshTokenTtlSeconds }
            { name: 'PORT',               value: '8000' }
            { name: 'ANTHROPIC_API_KEY',  secretRef: 'anthropic-api-key' }
            { name: 'ASOE_JWT_SECRET',    secretRef: 'asoe-jwt-secret' }
            // PARITY-4 — distinct signing key for attachment
            // capability tokens. Rotating attachment URL signing must
            // NOT invalidate auth sessions.
            { name: 'ASOE_ATTACHMENT_SIGNING_KEY', secretRef: 'asoe-attachment-signing-key' }
            { name: 'ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY', secretRef: 'asoe-attachment-signing-key-secondary' }
            { name: 'DATABASE_URL',       secretRef: 'database-url' }
            { name: 'REDIS_URL',          secretRef: 'redis-url' }
            // LangFuse (observability/langfuse_sink.py) — when all three
            // are non-empty the sink forwards every TraceRecord (incl.
            // per-LLM-call generation spans) to LangFuse. Empty values
            // make the sink a no-op; stdlib logging stays authoritative.
            { name: 'LANGFUSE_HOST',         value: langfuseHost }
            { name: 'LANGFUSE_PUBLIC_KEY',   secretRef: 'langfuse-public-key' }
            { name: 'LANGFUSE_SECRET_KEY',   secretRef: 'langfuse-secret-key' }
            // PARITY-5 — wires api/observability/otel.py to the
            // App Insights ingestion endpoint. When this is unset
            // (e.g. on Vercel), init_telemetry returns False and the
            // exporter chain stays cold.
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/v1/health'
                port: 8000
              }
              initialDelaySeconds: 20
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/v1/health'
                port: 8000
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-rule'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

// ──────────────────────────────────────────── Container App (ASOE UI)
//
// Stateless Next.js standalone bundle on port 3000. No sticky sessions
// (NextAuth's session is held in a JWT cookie, not in process memory).
// Reuses the same UAMI for ACR pull as the API app — the AcrPull role
// granted in Stage 1 covers both.

resource uiApp 'Microsoft.App/containerApps@2024-03-01' = if (deployUiContainerApp) {
  name: uiAppName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: uami.id
        }
      ]
      secrets: [
        {
          name: 'nextauth-secret'
          value: nextAuthSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'asoe-ui'
          image: containerImageUi
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            // NextAuth needs a canonical origin. Setting NEXTAUTH_URL
            // explicitly is more durable than relying on AUTH_TRUST_HOST
            // alone, which still requires NEXTAUTH_URL for callback
            // generation in some flows.
            { name: 'NEXTAUTH_URL',     value: 'https://${uiFqdn}' }
            { name: 'AUTH_TRUST_HOST',  value: 'true' }
            { name: 'NODE_ENV',         value: 'production' }
            { name: 'PORT',             value: '3000' }
            { name: 'NEXTAUTH_SECRET',  secretRef: 'nextauth-secret' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/'
                port: 3000
              }
              initialDelaySeconds: 30
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/'
                port: 3000
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
        rules: [
          {
            name: 'http-rule'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

// ───────────────────────────────────────────────────── Observability stack
//
// Two Container Apps that, together, deliver the ADR-039 §7.3 SLI
// surface on Azure without depending on Kubernetes or Azure Monitor's
// managed-Prometheus addon (which currently requires AKS + Container
// Insights). Both images bake the canonical configs from
// ops/observability/ at build time so the runtime container has no
// volume mounts to manage.
//
// Topology:
//   asoe-api (existing) — emits /api/v1/metrics in Prometheus text format.
//   asoe-prom (new)     — scrapes asoe-api over the managed-env internal
//                         network every 30s; 15-day TSDB retention.
//   asoe-graf (new)     — reads asoe-prom; provisions the SLI dashboard
//                         on first start. External ingress on 443 so an
//                         operator can hit it directly.
//
// Both apps are guarded by `deployObservability` so the API can deploy
// without the observability stack and vice versa. Min replicas = 0 so
// idle preprod environments cost nothing; scale.rules omitted because
// neither service handles bursty traffic that needs KEDA.

var apiInternalFqdn = deployContainerApp
  ? '${appName}.internal.${cae.properties.defaultDomain}'
  : ''

resource prometheusApp 'Microsoft.App/containerApps@2024-03-01' = if (deployObservability) {
  name: prometheusAppName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      activeRevisionsMode: 'Single'
      // Internal-only ingress — Grafana is the public-facing surface.
      ingress: {
        external: false
        targetPort: 9090
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'prometheus'
          image: prometheusImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'ASOE_ENVIRONMENT'
              value: asoeEnv
            }
            {
              // The scrape target. Container Apps internal DNS resolves
              // app names against the managed-env defaultDomain; port
              // 8000 matches Dockerfile.api EXPOSE.
              name: 'ASOE_API_TARGET'
              value: apiInternalFqdn != '' ? '${apiInternalFqdn}:8000' : 'asoe-api:8000'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/-/healthy'
                port: 9090
              }
              initialDelaySeconds: 30
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: deployObservability ? 1 : 0
        maxReplicas: 1
      }
    }
  }
}

resource grafanaApp 'Microsoft.App/containerApps@2024-03-01' = if (deployObservability) {
  name: grafanaAppName
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: uami.id
        }
      ]
      secrets: [
        {
          name: 'grafana-admin-password'
          value: empty(grafanaAdminPassword) ? 'admin' : grafanaAdminPassword
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'grafana'
          image: grafanaImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'GF_SECURITY_ADMIN_USER'
              value: 'admin'
            }
            {
              name: 'GF_SECURITY_ADMIN_PASSWORD'
              secretRef: 'grafana-admin-password'
            }
            {
              name: 'PROMETHEUS_URL'
              value: deployObservability ? 'http://${prometheusAppName}.internal.${cae.properties.defaultDomain}:9090' : ''
            }
            {
              name: 'LANGFUSE_BASE_URL'
              value: empty(grafanaLangfuseBaseUrl) ? langfuseHost : grafanaLangfuseBaseUrl
            }
            {
              name: 'LANGFUSE_PROJECT'
              value: grafanaLangfuseProject
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/health'
                port: 3000
              }
              initialDelaySeconds: 30
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: deployObservability ? 1 : 0
        maxReplicas: 1
      }
    }
  }
  dependsOn: [
    prometheusApp
  ]
}

// ──────────────────────────────── Retention sweeper Container Apps Job
//
// PARITY-8 — bulk-delete attachments past their per-tenant retention TTL.
// The Job runs api/retention_sweeper.py on a cron schedule against the
// same database the API app uses; reuses the API container image since
// the sweeper module is in the same package. RETENTION_SWEEPER_ENABLED
// is the second opt-in switch (defaulted off here, mirroring the kill-
// switch contract in api/retention_sweeper.is_enabled).
//
// Why a Job, not an in-process scheduler in the API app:
//   * the API replicas autoscale on HTTP traffic; a sweeper running in
//     every replica would multi-delete.
//   * Container Apps Jobs surface their own success/failure metrics and
//     can be paused with `az containerapp job stop` without touching
//     the API revision — operational blast-radius isolation.

resource retentionSweeperJob 'Microsoft.App/jobs@2024-03-01' = if (deployRetentionSweeperJob) {
  name: '${namePrefix}-retention-sweeper'
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      // Scheduled cron trigger — the runtime instantiates one
      // execution per cron tick.
      triggerType: 'Schedule'
      replicaTimeout: 3600  // 1h ceiling per execution
      replicaRetryLimit: 0  // a failed sweep does NOT auto-retry;
                            // operator inspects the audit log first
                            // (compliance review: silent retries on a
                            // bulk-delete job mask blast-radius bugs).
      scheduleTriggerConfig: {
        cronExpression: retentionSweeperCron
        parallelism: 1       // never more than one sweep at a time
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: uami.id
        }
      ]
      secrets: [
        { name: 'database-url', value: databaseUrl }
      ]
    }
    template: {
      containers: [
        {
          name: 'retention-sweeper'
          image: containerImage
          // Override the default API entrypoint with the sweeper CLI.
          // The CLI lives at scripts/run_retention_sweeper.py and
          // walks every tenant's candidates via
          // RetentionSweeper.dry_run → commit_with_residency_check.
          command: [ 'python' ]
          args: [ '-m', 'scripts.run_retention_sweeper' ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'ASOE_ENV', value: asoeEnv }
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            // Default OFF — the operator flips this to 'true' via
            // `az containerapp job update --set-env-vars
            // RETENTION_SWEEPER_ENABLED=true` after the dry-run
            // produces an audited plan the operator approves.
            { name: 'RETENTION_SWEEPER_ENABLED', value: 'false' }
          ]
        }
      ]
    }
  }
}

// ──────────────────────────────── Drift-alert forwarder Container Apps Job
//
// PARITY-7 followup — on every cron tick, scripts/run_drift_forwarder.py
// calls evaluate_all_extraction_drift() over the in-process metric
// snapshot and emits a structured extraction-drift log event per
// detected drift. The OTel exporter (api/observability/otel.py) forwards
// the events to App Insights so the Azure Monitor alert on
// `extraction-drift` has data without an additional ingestion hop.
//
// replicaRetryLimit=0 — a failed evaluator does NOT auto-retry; the
// operator inspects the structured error first (a silent retry on a
// drift signal masks real-time accuracy regressions).

resource driftAlertJob 'Microsoft.App/jobs@2024-03-01' = if (deployDriftAlertJob) {
  name: '${namePrefix}-drift-forwarder'
  location: location
  tags: commonTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 600  // 10min ceiling — the evaluator is in-process
      replicaRetryLimit: 0
      scheduleTriggerConfig: {
        cronExpression: driftAlertCron
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: '${acrName}.azurecr.io'
          identity: uami.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'drift-forwarder'
          image: containerImage
          command: [ 'python' ]
          args: [ '-m', 'scripts.run_drift_forwarder' ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'ASOE_ENV', value: asoeEnv }
            // App Insights connection string — same value the API
            // app uses, so the drift events flow to the same Log
            // Analytics workspace the rest of preprod uses.
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            // Evaluator window — see scripts/run_drift_forwarder.py
            // docstring + the parity plan's PARITY-7 §"Drift signal
            // alert". The defaults match the in-process evaluator's
            // own defaults; override per deployment if the 7-day
            // window needs tightening.
            { name: 'ASOE_DRIFT_RECENT_N', value: '50' }
            { name: 'ASOE_DRIFT_BASELINE_N', value: '200' }
          ]
        }
      ]
    }
  }
}

// ──────────────────────────────────────────────────────────────── Outputs

// Outputs that reference the conditional `app` / `uiApp` resources use
// ternaries so they degrade to empty strings during stages where the
// resource doesn't exist and surface the real values once it does.
output acrLoginServer       string = '${acrName}.azurecr.io'
output acrName              string = acrName
// Safe-access operator (`.?`) keeps the bicep linter (BCP318) happy when
// the resource is conditional — it short-circuits to null rather than
// failing the static null-check, and `?? ''` gives the empty-string
// fallback the deploy script already relies on.
output containerAppName     string = app.?name ?? ''
output containerAppFqdn     string = app.?properties.configuration.ingress.fqdn ?? ''
output uiContainerAppName   string = uiApp.?name ?? ''
output uiContainerAppFqdn   string = uiApp.?properties.configuration.ingress.fqdn ?? ''
output managedEnvDomain     string = cae.properties.defaultDomain
output postgresHost      string = pgServer.properties.fullyQualifiedDomainName
output postgresDatabase  string = pgDatabaseName
output postgresAdminUser string = pgAdminUser
output redisHost         string = redisEnterprise.properties.hostName
output redisSslPort      int    = redisDatabase.properties.port
output redisDatabaseName string = redisDatabase.name
output logAnalyticsId    string = logAnalytics.id
output uamiResourceId             string = uami.id
// Observability stack — empty strings until deployObservability=true.
output prometheusAppName  string = prometheusApp.?name ?? ''
output prometheusAppFqdn  string = prometheusApp.?properties.configuration.ingress.fqdn ?? ''
output grafanaAppName     string = grafanaApp.?name ?? ''
output grafanaAppFqdn     string = grafanaApp.?properties.configuration.ingress.fqdn ?? ''
output uamiPrincipalId            string = uami.properties.principalId
output uamiClientId               string = uami.properties.clientId
