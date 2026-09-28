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
  description = "Plain OWNER/REPO -- feeds the GCP module's assertion.repository condition."
  type        = string
  default     = "thararaoganji/remediation-agent"
}

variable "azure_github_repo" {
  description = <<-EOT
    Exactly what GitHub's OIDC token puts in the `sub` claim for this
    repo -- confirmed from a live token (Azure logs the presented
    subject on a mismatch) to include stable owner/repo IDs, not just
    the plain names. See infra/modules/azure/variables.tf's github_repo
    for how to re-derive this if it ever needs to change.
  EOT
  type        = string
  default     = "thararaoganji@176417932/remediation-agent@1322380487"
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
