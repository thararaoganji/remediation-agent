variable "azure_subscription_id" {
  type    = string
  default = "69cf5bbc-a9a8-41e2-861c-b3e6f78ad335"
}

variable "azure_location" {
  type    = string
  default = "eastus"
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

variable "azure_dashboard_image" {
  type    = string
  default = "sonarremediationsandboxacr.azurecr.io/sonar-dashboard:latest"
}

variable "azure_agent_image" {
  type    = string
  default = "sonarremediationsandboxacr.azurecr.io/sonar-remediation-agent:latest"
}
