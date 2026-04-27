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

@description('JWT secret for ASOE auth. Placeholder until set-secrets.sh is run.')
@secure()
param asoeJwtSecret string = 'placeholder-set-via-set-secrets-sh'

@description('PostgreSQL connection string. Placeholder until set-secrets.sh is run.')
@secure()
param databaseUrl string = 'placeholder-set-via-set-secrets-sh'

@description('Redis connection string. Placeholder until set-secrets.sh is run.')
@secure()
param redisUrl string = 'placeholder-set-via-set-secrets-sh'

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
  tags: commonTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
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
resource pgFwAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: pgServer
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

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
          name: 'asoe-jwt-secret'
          value: asoeJwtSecret
        }
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'redis-url'
          value: redisUrl
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
            { name: 'ASOE_KILL_SWITCH',   value: '0' }
            { name: 'ASOE_EXPLAIN_MODE',  value: '0' }
            { name: 'CORS_ALLOWED_ORIGIN',        value: corsAllowedOrigin }
            { name: 'CORS_ALLOWED_ORIGINS',       value: corsAllowedFinalCsv }
            { name: 'CORS_ALLOWED_ORIGIN_REGEX',  value: corsAllowedOriginRegex }
            { name: 'PORT',               value: '8000' }
            { name: 'ANTHROPIC_API_KEY',  secretRef: 'anthropic-api-key' }
            { name: 'ASOE_JWT_SECRET',    secretRef: 'asoe-jwt-secret' }
            { name: 'DATABASE_URL',       secretRef: 'database-url' }
            { name: 'REDIS_URL',          secretRef: 'redis-url' }
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

// ──────────────────────────────────────────────────────────────── Outputs

// Outputs that reference the conditional `app` / `uiApp` resources use
// ternaries so they degrade to empty strings during stages where the
// resource doesn't exist and surface the real values once it does.
output acrLoginServer       string = '${acrName}.azurecr.io'
output acrName              string = acrName
output containerAppName     string = deployContainerApp ? app.name : ''
output containerAppFqdn     string = deployContainerApp ? app.properties.configuration.ingress.fqdn : ''
output uiContainerAppName   string = deployUiContainerApp ? uiApp.name : ''
output uiContainerAppFqdn   string = deployUiContainerApp ? uiApp.properties.configuration.ingress.fqdn : ''
output managedEnvDomain     string = cae.properties.defaultDomain
output postgresHost      string = pgServer.properties.fullyQualifiedDomainName
output postgresDatabase  string = pgDatabaseName
output postgresAdminUser string = pgAdminUser
output redisHost         string = redisEnterprise.properties.hostName
output redisSslPort      int    = redisDatabase.properties.port
output redisDatabaseName string = redisDatabase.name
output logAnalyticsId    string = logAnalytics.id
output uamiResourceId             string = uami.id
output uamiPrincipalId            string = uami.properties.principalId
output uamiClientId               string = uami.properties.clientId
