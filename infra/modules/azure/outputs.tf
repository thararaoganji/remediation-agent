output "dashboard_url" {
  value = "https://${azurerm_container_app.dashboard.ingress[0].fqdn}"
}

output "acr_login_server" {
  value = azurerm_container_registry.acr.login_server
}

output "resource_group_name" {
  value = azurerm_resource_group.main.name
}

# The four values needed for AZURE_CLIENT_ID / AZURE_TENANT_ID /
# AZURE_SUBSCRIPTION_ID GitHub Environment variables.
output "github_deployer_client_id" {
  value = azuread_application.github_deployer.client_id
}

output "tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}
