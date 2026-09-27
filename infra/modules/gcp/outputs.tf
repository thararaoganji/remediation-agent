output "dashboard_url" {
  value = google_cloud_run_v2_service.dashboard.uri
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}"
}

output "dashboard_sa_email" {
  value = google_service_account.dashboard_sa.email
}

# The four values needed for GCP_WIF_PROVIDER / GCP_DEPLOY_SERVICE_ACCOUNT
# GitHub Environment variables -- see infra/environments/*/outputs.tf,
# which surfaces these one level up for `gh` commands to read directly.
output "wif_provider" {
  value = "projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/providers/${google_iam_workload_identity_pool_provider.github.workload_identity_pool_provider_id}"
}

output "github_deployer_sa_email" {
  value = google_service_account.github_deployer.email
}
