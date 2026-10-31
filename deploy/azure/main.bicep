// Tokan enterprise gateway — Azure Container Apps deployment.
//
// Deploys: ACA environment + gateway container app, Azure Cache for Redis,
// Key Vault (+ placeholder secrets). The gateway proxies OpenAI-compatible
// clients (VS Code Copilot BYOK, SDKs, agents) and runs the optimization
// pipeline (AST compaction, semantic cache, adaptive routing) before
// forwarding upstream.
//
// Auth note: upstreamApiKey is a deployment parameter (marked @secure(), so
// its value is redacted from deployment history) and is passed to the
// container as `--api-key`, which makes the gateway REPLACE any client's
// Authorization header before forwarding upstream. For stricter secret
// handling, move it to Key Vault and switch the container to an env-var
// secret reference.

param location string = resourceGroup().location
param appName string = 'tokan-gateway'
// Container image to run the gateway from (your own ACR/EH repo; no default
// so registry endpoints are never baked into the template).
param containerImage string

// Upstream OpenAI-compatible endpoint the gateway forwards to, and the
// credential the gateway injects upstream (clients need none). Both are
// deployment-specific: pass them at deploy time, never commit them.
param upstreamUrl string
@secure()
param upstreamApiKey string

// Session metrics are per-process in-memory; >1 replica would serve
// inconsistent dashboard views. Scale-out requires centralized metrics
// (e.g. App Insights) first — keep 1/1 until then.
param minReplicas int = 1
param maxReplicas int = 1

param redisName string = 'tokan-redis'
param keyVaultName string = 'tokan-kv'
param openaiKeyName string = 'openai-api-key'
param anthropicKeyName string = 'anthropic-api-key'

resource redisCache 'Microsoft.Cache/redis@2024-11-01' = {
  name: redisName
  location: location
  properties: {
    sku: { name: 'Standard', family: 'C', capacity: 1 }
    redisConfiguration: { 'notify-keyspace-events': 'Ex' }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    accessPolicies: []
    enableSoftDelete: true
  }
}

resource openAiSecret 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: keyVault
  name: openaiKeyName
  properties: {
    value: 'REPLACE_WITH_OPENAI_KEY'
    contentType: 'text/plain'
  }
}

resource anthropicSecret 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: keyVault
  name: anthropicKeyName
  properties: {
    value: 'REPLACE_WITH_ANTHROPIC_KEY'
    contentType: 'text/plain'
  }
}

resource containerAppEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    zoneRedundant: false
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  properties: {
    environmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 4040
        traffic: [{ weight: 100, latestRevision: true }]
      }
    }
    template: {
      containers: [
        {
          name: 'gateway'
          image: containerImage
          command: ['python']
          args: [
            '-m'
            'tokan.cli'
            'serve'
            '--host'
            '0.0.0.0'
            '--port'
            '4040'
            '--upstream'
            upstreamUrl
            '--api-key'
            upstreamApiKey
            '--prompt-cache-key'
            '--log-requests'
          ]
          env: [
            { name: 'REDIS_URL', value: 'redis://${redisCache.properties.hostName}:${redisCache.properties.sslPort}/0' }
            { name: 'KEYVAULT_URL', value: 'https://${keyVault.properties.vaultUri}' }
            { name: 'TOKAN_TIER', value: 'production' }
          ]
          resources: {
            cpu: 2
            memory: '4Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 4040 }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 4040 }
              initialDelaySeconds: 5
              periodSeconds: 5
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scale-rule'
            custom: {
              type: 'http'
              metadata: { concurrentRequests: '50' }
            }
          }
        ]
      }
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

output gatewayUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
