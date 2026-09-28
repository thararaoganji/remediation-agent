terraform {
  required_version = ">= 1.6"

  # State lives in Azure Storage, not GCS -- deliberately, so an Azure
  # deploy has zero GCP touchpoint at all, not even for state. Auth is
  # keyless: use_azuread_auth reads/writes the state blob via RBAC
  # (Storage Blob Data Contributor on the storage account) instead of a
  # storage account key; use_oidc reuses the same federated-credential
  # token azure/login already sets up in CI, so no separate secret is
  # needed just to unlock the backend.
  backend "azurerm" {
    use_azuread_auth = true
    use_oidc         = true
    # resource_group_name/storage_account_name/container_name/key supplied
    # via `-backend-config` at `tofu init` time, same pattern as the GCP
    # roots' gcs backend.
  }
}

provider "azurerm" {
  subscription_id = var.azure_subscription_id
  features {
    key_vault {
      purge_soft_delete_on_destroy = true
    }
  }
}

provider "azuread" {}
