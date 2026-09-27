output "gcp_dashboard_url" {
  value = module.gcp.dashboard_url
}

output "gcp_wif_provider" {
  value = module.gcp.wif_provider
}

output "gcp_github_deployer_sa_email" {
  value = module.gcp.github_deployer_sa_email
}

output "azure_dashboard_url" {
  value = module.azure.dashboard_url
}

output "azure_github_deployer_client_id" {
  value = module.azure.github_deployer_client_id
}

output "azure_tenant_id" {
  value = module.azure.tenant_id
}

output "azure_resource_group_name" {
  value = module.azure.resource_group_name
}
