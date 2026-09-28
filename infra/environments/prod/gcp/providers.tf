terraform {
  required_version = ">= 1.6"

  backend "gcs" {
    # bucket/prefix supplied via `-backend-config` at `tofu init` time.
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}
