terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

data "azurerm_client_config" "current" {}

locals {
  rg_name = "sonar-remediation-${var.environment}-rg"
  # ACR names: globally unique, alphanumeric only, no hyphens.
  acr_name = "sonarremediation${var.environment}acr"
  # Key Vault names: globally unique, 3-24 chars, alphanumeric + hyphens.
  kv_name = "sonar-remed-${var.environment}-kv"
}

resource "azurerm_resource_group" "main" {
  name     = local.rg_name
  location = var.location
  tags     = { environment = var.environment }
}

# --- Container Registry -------------------------------------------------------

resource "azurerm_container_registry" "acr" {
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  tags                = { environment = var.environment }
}

# --- Cosmos DB (storage_azure.py's DocumentStore) -----------------------------
# Container/partition-key shapes must match storage_azure.py's CosmosStore
# exactly: /id for every collection except events, which is queried by
# run_id (event_stream_azure.py's polling query), so it's partitioned on
# /run_id instead.

resource "azurerm_cosmosdb_account" "main" {
  name                = "sonar-remediation-${var.environment}-cosmos"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.main.location
    failover_priority = 0
  }

  tags = { environment = var.environment }
}

resource "azurerm_cosmosdb_sql_database" "dashboard" {
  name                = "dashboard"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
}

resource "azurerm_cosmosdb_sql_container" "by_id" {
  for_each            = toset(["users", "sonar_servers", "github_credentials", "llm_configs", "runs"])
  name                = each.key
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.dashboard.name
  partition_key_paths = ["/id"]
}

resource "azurerm_cosmosdb_sql_container" "events" {
  name                = "events"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.dashboard.name
  partition_key_paths = ["/run_id"]
}

# --- Key Vault (secrets_azure.py's SecretStore) -------------------------------

resource "azurerm_key_vault" "main" {
  name                       = local.kv_name
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 7
  tags                       = { environment = var.environment }
}

resource "random_password" "session_secret" {
  length  = 64
  special = false
}

resource "azurerm_key_vault_secret" "session_secret" {
  name         = "dashboard-session-secret"
  value        = random_password.session_secret.result
  key_vault_id = azurerm_key_vault.main.id
  # github_deployer_kv_admin (below) grants the identity that actually runs
  # `tofu apply` in CI its own Key Vault Secrets Officer access -- this used
  # to instead self-grant "whoever's running this" Key Vault Administrator
  # (a control-plane role), which broke the first time CI itself (rather
  # than a human) ran apply: github_deployer had no
  # Microsoft.Authorization/roleAssignments/delete rights on this vault, so
  # replacing that self-granted binding 403'd. A dedicated, stable grant for
  # the actual CI identity is both simpler and avoids that churn.
  depends_on = [azurerm_role_assignment.github_deployer_kv_admin]
}

# --- Managed identities (least-privilege split, mirrors the GCP SAs) ---------

resource "azurerm_user_assigned_identity" "agent" {
  name                = "sonar-agent-identity"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = { environment = var.environment }
}

resource "azurerm_user_assigned_identity" "dashboard" {
  name                = "sonar-dashboard-identity"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = { environment = var.environment }
}

resource "azurerm_cosmosdb_sql_role_assignment" "agent_cosmos" {
  # Cosmos DB data-plane RBAC is a separate system from Azure RBAC --
  # "Cosmos DB Built-in Data Contributor" isn't a role azurerm_role_assignment
  # can resolve; it's a Cosmos SQL role, granted through this resource
  # against the well-known built-in role definition id.
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  role_definition_id  = "${azurerm_cosmosdb_account.main.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_user_assigned_identity.agent.principal_id
  scope               = azurerm_cosmosdb_account.main.id
}

resource "azurerm_role_assignment" "agent_kv_read" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

resource "azurerm_cosmosdb_sql_role_assignment" "dashboard_cosmos" {
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  role_definition_id  = "${azurerm_cosmosdb_account.main.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_user_assigned_identity.dashboard.principal_id
  scope               = azurerm_cosmosdb_account.main.id
}

resource "azurerm_role_assignment" "dashboard_kv_admin" {
  # Officer, not User -- the dashboard CREATES a new secret per Sonar
  # server/GitHub credential/LLM config (secrets_azure.py's
  # create_secret_with_value), not just reads one. Matches the exact
  # secretAccessor-vs-admin lesson from the GCP deployment's own
  # OWASP-review fix earlier this session.
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = azurerm_user_assigned_identity.dashboard.principal_id
}

resource "azurerm_role_assignment" "dashboard_container_apps" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Container Apps Contributor"
  principal_id         = azurerm_user_assigned_identity.dashboard.principal_id
}

# The container/job `registry { identity = ... }` blocks below name which
# identity Container Apps should use to pull the image, but naming it
# there doesn't grant it anything -- confirmed the hard way, the first
# real apply with a genuine image still failed with "unable to pull image
# using Managed identity" until these existed. github_deployer's AcrPush
# (below) is a separate identity, for CI pushing at build time, not for
# either of these two pulling at run time.
resource "azurerm_role_assignment" "agent_acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.agent.principal_id
}

resource "azurerm_role_assignment" "dashboard_acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.dashboard.principal_id
}

# --- Container Apps environment, jobs, and the dashboard app -----------------

resource "azurerm_container_app_environment" "main" {
  name                = "sonar-remediation-${var.environment}-env"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = { environment = var.environment }
}

resource "azurerm_container_app_job" "agent" {
  for_each                     = toset(["techdebt", "coverage", "duplicate"])
  name                         = "sonar-remediation-${each.key}-job"
  resource_group_name          = azurerm_resource_group.main.name
  location                     = azurerm_resource_group.main.location
  container_app_environment_id = azurerm_container_app_environment.main.id
  replica_timeout_in_seconds   = 3600
  replica_retry_limit          = 0

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.agent.id]
  }

  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.agent.id
  }

  template {
    container {
      name  = "agent"
      image = var.agent_image
      # Consumption-plan Container Apps only accept a fixed set of
      # cpu:memory ratios (2:1 GiB per vCPU) -- 2 vCPU must pair with 4Gi,
      # not 2Gi (confirmed the hard way: ContainerAppInvalidResourceTotal).
      # GCP's Cloud Run Job has no such restriction, hence the asymmetry
      # with that module's cpu=2/memory=2Gi.
      cpu    = 2.0
      memory = "4Gi"

      env {
        name  = "AGENT_TYPE"
        value = each.key
      }
      env {
        name  = "CLOUD_PROVIDER"
        value = "azure"
      }
      env {
        name  = "AZURE_COSMOS_ENDPOINT"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      env {
        name  = "AZURE_KEY_VAULT_URL"
        value = azurerm_key_vault.main.vault_uri
      }
      # Baked-in fallback values -- every real run overrides these
      # per-execution from runs.py's create_run(), same as the GCP jobs.
      env {
        name  = "SONAR_BASE_URL"
        value = "http://placeholder:9000"
      }
      env {
        name  = "GITHUB_REPO"
        value = "placeholder/placeholder"
      }
      env {
        name  = "LANGUAGE"
        value = "java"
      }
      env {
        name  = "CE_EDITION"
        value = "true"
      }
    }
  }
}

resource "azurerm_container_app" "dashboard" {
  name                         = "sonar-dashboard"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  tags                         = { environment = var.environment }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.dashboard.id]
  }

  registry {
    server   = azurerm_container_registry.acr.login_server
    identity = azurerm_user_assigned_identity.dashboard.id
  }

  template {
    min_replicas = 0
    max_replicas = 3
    container {
      name   = "dashboard"
      image  = var.dashboard_image
      cpu    = 1.0
      memory = "2Gi"

      env {
        name  = "CLOUD_PROVIDER"
        value = "azure"
      }
      env {
        name  = "AZURE_SUBSCRIPTION_ID"
        value = var.subscription_id
      }
      env {
        name  = "AZURE_RESOURCE_GROUP"
        value = azurerm_resource_group.main.name
      }
      env {
        name  = "AZURE_COSMOS_ENDPOINT"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      env {
        name  = "AZURE_KEY_VAULT_URL"
        value = azurerm_key_vault.main.vault_uri
      }
      env {
        name  = "COOKIE_SECURE"
        value = "true"
      }
      env {
        name        = "SESSION_SECRET_KEY"
        secret_name = "session-secret-key"
      }
    }
  }

  secret {
    name                = "session-secret-key"
    key_vault_secret_id = azurerm_key_vault_secret.session_secret.versionless_id
    identity            = azurerm_user_assigned_identity.dashboard.id
  }

  ingress {
    external_enabled = true
    target_port      = 8080
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

# --- GitHub Actions OIDC (Azure AD federated credential) ---------------------
# Keyless, same reasoning as the GCP WIF setup -- GitHub's own OIDC token is
# exchanged for a short-lived Azure AD token, no client secret stored in
# GitHub. Subject is scoped to this repo's specific GitHub Environment
# (sandbox/prod), matching one-to-one with the two GitHub Environments
# this whole setup uses for the approval-gate split.

resource "azuread_application" "github_deployer" {
  display_name = "sonar-remediation-github-deployer-${var.environment}"
}

resource "azuread_service_principal" "github_deployer" {
  client_id = azuread_application.github_deployer.client_id
}

resource "azuread_application_federated_identity_credential" "github" {
  application_id = azuread_application.github_deployer.id
  display_name   = "github-actions-${var.environment}"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:${var.github_repo}:environment:${var.environment}"
}

resource "azurerm_role_assignment" "github_deployer_rg" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.github_deployer.object_id
}

resource "azurerm_role_assignment" "github_deployer_acr_push" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPush"
  principal_id         = azuread_service_principal.github_deployer.object_id
}

resource "azurerm_role_assignment" "github_deployer_kv_admin" {
  # Contributor (github_deployer_rg, above) is control-plane only --
  # reading/writing the actual SECRET VALUE in azurerm_key_vault_secret
  # is a Key Vault data-plane action, same distinction as the Cosmos DB
  # SQL role split elsewhere in this module. The first real CI apply
  # proved this: `tofu plan` failed reading dashboard-session-secret with
  # a 403 on Microsoft.KeyVault/vaults/secrets/getSecret/action.
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = azuread_service_principal.github_deployer.object_id
}
