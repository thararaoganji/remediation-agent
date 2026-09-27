variable "gcp_project_id" {
  type    = string
  default = "aiproject-495122"
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "azure_subscription_id" {
  type    = string
  default = "69cf5bbc-a9a8-41e2-861c-b3e6f78ad335"
}

variable "azure_location" {
  type    = string
  default = "eastus"
}

variable "github_repo" {
  type    = string
  default = "thararaoganji/remediation-agent"
}

variable "gcp_dashboard_image" {
  description = "Supplied by CI per-deploy (git-SHA tagged); has a working default for a manual first apply."
  type        = string
  default     = "us-central1-docker.pkg.dev/aiproject-495122/sonar-remediation-repo/sonar-dashboard:latest"
}

variable "gcp_agent_image" {
  type    = string
  default = "us-central1-docker.pkg.dev/aiproject-495122/sonar-remediation-repo/sonar-remediation-agent:latest"
}

variable "azure_dashboard_image" {
  description = "Full ACR reference -- no working default until the first image is pushed to this environment's own ACR (see infra/README.md's bootstrap order)."
  type        = string
  default     = "sonarremediationprodacr.azurecr.io/sonar-dashboard:latest"
}

variable "azure_agent_image" {
  type    = string
  default = "sonarremediationprodacr.azurecr.io/sonar-remediation-agent:latest"
}
