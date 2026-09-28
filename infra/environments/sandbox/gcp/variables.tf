variable "gcp_project_id" {
  type    = string
  default = "sonar-remediation-sandbox"
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "github_repo" {
  description = "Plain OWNER/REPO -- feeds the GCP module's assertion.repository condition."
  type        = string
  default     = "thararaoganji/remediation-agent"
}

variable "gcp_dashboard_image" {
  type    = string
  default = "us-central1-docker.pkg.dev/sonar-remediation-sandbox/sonar-remediation-repo/sonar-dashboard:latest"
}

variable "gcp_agent_image" {
  type    = string
  default = "us-central1-docker.pkg.dev/sonar-remediation-sandbox/sonar-remediation-repo/sonar-remediation-agent:latest"
}
