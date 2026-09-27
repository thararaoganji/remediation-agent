variable "subscription_id" {
  type = string
}

variable "location" {
  type    = string
  default = "eastus"
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["sandbox", "prod"], var.environment)
    error_message = "environment must be \"sandbox\" or \"prod\"."
  }
}

variable "github_repo" {
  description = "OWNER/REPO -- scopes the federated credential's subject to this repo's GitHub Environments."
  type        = string
}

variable "dashboard_image" {
  description = "Full ACR image reference (with tag) for the sonar-dashboard Container App."
  type        = string
}

variable "agent_image" {
  description = "Full ACR image reference (with tag) for the shared agent image used by all three Container Apps Jobs."
  type        = string
}
