module "azure" {
  source = "../../../modules/azure"

  subscription_id = var.azure_subscription_id
  location        = var.azure_location
  environment     = "sandbox"
  github_repo     = var.azure_github_repo

  dashboard_image = var.azure_dashboard_image
  agent_image     = var.azure_agent_image
}
