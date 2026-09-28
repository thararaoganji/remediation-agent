module "gcp" {
  source = "../../../modules/gcp"

  project_id  = var.gcp_project_id
  region      = var.gcp_region
  environment = "prod"
  github_repo = var.github_repo

  dashboard_image = var.gcp_dashboard_image
  agent_image     = var.gcp_agent_image

  # Imported secret already signs every live session -- Tofu must never
  # rotate its value. See infra/modules/gcp/variables.tf's docstring.
  manage_session_secret_version = false

  # An accidental destroy-and-recreate here would take down the real,
  # currently-serving dashboard and agents.
  deletion_protection = true
}
