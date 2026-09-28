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
  description = <<-EOT
    Exactly the OWNER/REPO string GitHub puts in the OIDC token's `sub`
    claim for this repo -- NOT necessarily the plain "owner/repo" you'd
    expect. GitHub can include stable numeric IDs here
    ("owner@ownerId/repo@repoId") to survive renames/transfers; confirm
    the real value from a failed run's error (Azure logs the exact
    presented subject) or `gh api repos/OWNER/REPO -q
    '"\(.owner.login)@\(.owner.id)/\(.name)@\(.id)"'` rather than
    assuming. This is unrelated to the GCP module's identically-named
    variable, which scopes a different claim (`assertion.repository`,
    always plain) and is unaffected by this.
  EOT
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
