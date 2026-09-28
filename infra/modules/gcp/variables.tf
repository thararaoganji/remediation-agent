variable "project_id" {
  description = "GCP project this environment's resources live in."
  type        = string
}

variable "region" {
  description = "GCP region for every regional resource (Artifact Registry, Cloud Run)."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "sandbox | prod -- used only for resource labels, never for naming (names match what's already live in prod)."
  type        = string
  validation {
    condition     = contains(["sandbox", "prod"], var.environment)
    error_message = "environment must be \"sandbox\" or \"prod\"."
  }
}

variable "github_repo" {
  description = "OWNER/REPO this Workload Identity Federation provider trusts -- scopes which repo's Actions runs can impersonate github-deployer."
  type        = string
}

variable "dashboard_image" {
  description = "Full image reference (with tag/digest) for the sonar-dashboard Cloud Run Service."
  type        = string
}

variable "agent_image" {
  description = "Full image reference (with tag/digest) for the shared agent image used by all three Cloud Run Jobs."
  type        = string
}

variable "manage_session_secret_version" {
  description = <<-EOT
    Whether Tofu is allowed to write dashboard-session-secret's VALUE.
    False for prod (the secret is imported; its real value already signs
    every live user's session -- Tofu must never rotate it out from under
    them). True for sandbox (created fresh, nothing depends on it yet).
  EOT
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = <<-EOT
    Passed to the Cloud Run Jobs and Service. The provider defaults this
    to true, which silently blocks any `tofu apply` that needs to
    destroy-and-recreate one of these (confirmed the hard way: a tainted
    resource -- e.g. from a bad image -- couldn't be replaced until this
    was explicitly set false). False for sandbox, where that churn is
    expected; true for prod, where an accidental destroy would take down
    the live dashboard/agents.
  EOT
  type        = bool
  default     = false
}
