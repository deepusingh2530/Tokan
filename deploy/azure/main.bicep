// Tokan enterprise gateway — Azure Container Apps deployment.
//
// Deploys: ACA environment + gateway container app, Azure Cache for Redis,
// Key Vault (+ placeholder secrets). The gateway proxies OpenAI-compatible
// clients (VS Code Copilot BYOK, SDKs, agents) and runs the optimization
// pipeline (AST compaction, semantic cache, adaptive routing) before
// forwarding upstream.
//
// Auth note: the upstream key and the inbound keys never appear in container
// args. They are written into Key Vault as secrets (from @secure() params,
// redacted from deployment history), and the container reads them from env
// vars backed by Key Vault references — which requires a user-assigned
// managed identity with `get` on the vault. `--api-key` makes the gateway
// REPLACE any client's Authorization header before forwarding upstream.
//
// M6 hardening: inbound auth, secret posture, quality gate, routing wire-up.
// - inboundApiKeys: comma-separated keys exposed as env TOKAN_API_KEYS.
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

// M6 inbound auth: comma-separated gateway keys, written into Key Vault and
// exposed to the container as env TOKAN_API_KEYS (Key Vault reference).
// Clients send one as Authorization: Bearer <key> or
// X-Optimizer-Api-Key: <key>. Either candidate is compared with
// hmac.compare_digest (constant-time). /healthz stays open for probes;
// everything else requires a key when this is non-empty.
// Empty = no inbound auth, which is fine on the default loopback bind.
@secure()
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
// App-scoped secret names the container references from env. These are the
// names in `configuration.secrets` (ACA side), not in Key Vault.
param upstreamSecretName string = 'tokan-upstream-key'
param gatewayKeysSecretName string = 'tokan-gateway-keys'

// User-assigned identity the container app uses to read Key Vault secrets.
// User-assigned (not system-assigned) so its principalId exists before the
// vault access policy and the container app are both created — a
// system-assigned identity would create a dependency cycle (vault policy
// needs the app, the app's env needs the vault).
resource gatewayIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-identity'
  location: location
}

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
    // The gateway's user-assigned identity gets read-only secret access —
    // enough to resolve the two env references, not enough to rotate.
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: gatewayIdentity.properties.principalId
        permissions: { secrets: ['get', 'list'] }
      }
    ]
    enableSoftDelete: true
  }
}

// The upstream key lands in Key Vault straight from the @secure() param, so
// the secret value is never materialized in the template, deployment history,
// or container args — the container only ever sees the env reference.
resource openAiSecret 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: keyVault
  name: openaiKeyName
  properties: {
    value: upstreamApiKey
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

// Inbound gateway keys also live in Key Vault (the container reads them via
// env TOKAN_API_KEYS). Key Vault accepts an empty value, which matches the
// gateway's "empty = no inbound auth" contract — deliberately NOT replaced
// by a placeholder, since a known placeholder value would become a working,
// publicly-known gateway key.
resource gatewayKeysSecret 'Microsoft.KeyVault/vaults/secrets@2024-04-01-preview' = {
  parent: keyVault
  name: 'tokan-gateway-keys'
  properties: {
    value: inboundApiKeys
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
  identity: {
    // User-assigned identity that resolves the Key Vault secret references
    // below. Declared above as `gatewayIdentity`.
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${gatewayIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 4040
        traffic: [{ weight: 100, latestRevision: true }]
      }
      // Secrets the container references by name from env. The two gateway
      // keys are Key Vault references: the value is resolved at container
      // start via the managed identity and never stored in the app config.
      secrets: [
        {
          name: upstreamSecretName
          keyVaultUrl: openAiSecret.properties.secretUriWithVersion
          identity: gatewayIdentity.id
        }
        {
          name: gatewayKeysSecretName
          keyVaultUrl: gatewayKeysSecret.properties.secretUriWithVersion
          identity: gatewayIdentity.id
        }
      ]
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
          //
          // No --api-key / --api-keys here on purpose: both credentials come
          // from env (Key Vault references) so they never appear in the
          // revision spec, `az containerapp show`, or deployment history.
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
            '--prompt-cache-key'
            '--log-requests'
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
            // Secrets via Key Vault references (see configuration.secrets).
            // TOKAN_UPSTREAM_API_KEY is the upstream credential `serve` uses
            // as the --api-key default; TOKAN_API_KEYS holds the inbound
            // gateway keys. Feature flags are CLI args only — they are not
            // env-configurable, so no env mirror.
            { name: 'TOKAN_UPSTREAM_API_KEY', secretRef: upstreamSecretName }
            { name: 'TOKAN_API_KEYS', secretRef: gatewayKeysSecretName }
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
}

output gatewayUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'