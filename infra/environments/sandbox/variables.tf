variable "gcp_project_id" {
  type    = string
  default = "sonar-remediation-sandbox"
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
  type    = string
  default = "us-central1-docker.pkg.dev/sonar-remediation-sandbox/sonar-remediation-repo/sonar-dashboard:latest"
}

variable "gcp_agent_image" {
  type    = string
  default = "us-central1-docker.pkg.dev/sonar-remediation-sandbox/sonar-remediation-repo/sonar-remediation-agent:latest"
}

variable "azure_dashboard_image" {
  type    = string
  default = "sonarremediationsandboxacr.azurecr.io/sonar-dashboard:latest"
}

variable "azure_agent_image" {
  type    = string
  default = "sonarremediationsandboxacr.azurecr.io/sonar-remediation-agent:latest"
}
