terraform {
  required_version = ">= 1.6"

  # See infra/environments/sandbox/azure/providers.tf's comment -- same
  # keyless Azure Storage backend, separate state key.
  backend "azurerm" {
    use_azuread_auth = true
    use_oidc         = true
  }
}

provider "azurerm" {
  subscription_id = var.azure_subscription_id
  features {
    key_vault {
      # Unlike sandbox, don't purge on destroy -- a soft-deleted prod
      # vault should require a deliberate manual purge, not happen
      # automatically as a side effect of some other Tofu operation.
      purge_soft_delete_on_destroy = false
    }
  }
}

provider "azuread" {}
