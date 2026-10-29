param location string = resourceGroup().location
param appName string = 'tokan-gateway'
param containerImage string
param minReplicas int = 2
param maxReplicas int = 10
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
  dependsOn: [containerAppEnv]
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
