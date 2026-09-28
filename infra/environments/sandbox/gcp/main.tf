module "gcp" {
  source = "../../../modules/gcp"

  project_id  = var.gcp_project_id
  region      = var.gcp_region
  environment = "sandbox"
  github_repo = var.github_repo

  dashboard_image = var.gcp_dashboard_image
  agent_image     = var.gcp_agent_image

  # Fresh secret, nothing depends on it yet -- safe for Tofu to manage its
  # value directly, unlike prod's imported one.
  manage_session_secret_version = true
}
