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
//
// M6 hardening: inbound auth, secret posture, quality gate, routing wire-up.
// - inboundApiKeys: comma-separated keys for --api-keys / TOKAN_API_KEYS.
//   Clients present as Authorization: Bearer <key> or X-Optimizer-Api-Key: <key>.
//   Empty = no inbound auth (loopback-only binds, fail-closed without keys).
// - redactSecrets: opt-in replaces detected secrets with [REDACTED:<type>]
//   before canonicalization, cache digest, and upstream call.
// - docFallbackFull: quality gate — low-coverage doc asks send the full document.
// - taskRouter: classify requests into tier_0/1/2 and disclose on receipts/headers/metrics.
// - tier1Model: explicit model rewrite for tier_1 requests (never defaulted).
// - upstreamApiKey: upstream/injectable key (marked @secure(), kept separate
//   from inbound auth so BYOK/plan-gated flows keep working).

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

// M6 inbound auth: comma-separated gateway keys (env TOKAN_API_KEYS).
// Clients send one as Authorization: Bearer <key> or
// X-Optimizer-Api-Key: <key>. Either candidate is compared with
// hmac.compare_digest (constant-time). /healthz stays open for probes;
// everything else requires a key when this is non-empty.
// Empty = no inbound auth, which is fine on the default loopback bind.
param inboundApiKeys string = ''

// M6 feature flags (all default off).
// --redact-secrets: replace detected secrets before anything leaves the machine.
// --doc-fallback-full: quality gate — low-coverage doc asks send the full document.
// --task-router: classify each request into a routing tier and disclose it.
// --tier1-model: explicit model rewrite for tier_1 requests (never defaulted).
param redactSecrets bool = false
param docFallbackFull bool = false
param taskRouter bool = false
param tier1Model string = ''

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
          // Flags are appended conditionally: Bicep arrays are homogeneously
          // typed (all strings), so boolean params expand to either their
          // flag string or nothing at all — an empty-string arg would reach
          // argparse as a stray positional.
          args: concat([
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
            '--api-keys'
            inboundApiKeys
          ],
          redactSecrets ? ['--redact-secrets'] : [],
          docFallbackFull ? ['--doc-fallback-full'] : [],
          taskRouter ? ['--task-router'] : [],
          tier1Model != '' ? ['--tier1-model', tier1Model] : []
          )
          env: [
            { name: 'REDIS_URL', value: 'redis://${redisCache.properties.hostName}:${redisCache.properties.sslPort}/0' }
            { name: 'KEYVAULT_URL', value: 'https://${keyVault.properties.vaultUri}' }
            { name: 'TOKAN_TIER', value: 'production' }
            // M6: inbound auth keys (read by `serve` as --api-keys default;
            // also passed explicitly in args above). Feature flags are CLI
            // args only — they are not env-configurable, so no env mirror.
            { name: 'TOKAN_API_KEYS', value: inboundApiKeys }
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