terraform {
  required_version = ">= 1.6"

  backend "gcs" {
    # bucket/prefix supplied via `-backend-config` at `tofu init` time (see
    # infra/README.md) -- kept out of this file since the same file is
    # reused unchanged by CI regardless of which state bucket a given
    # setup uses.
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

provider "azurerm" {
  subscription_id = var.azure_subscription_id
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
}

provider "azuread" {}
