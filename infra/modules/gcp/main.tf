terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

data "google_project" "current" {
  project_id = var.project_id
}

# --- Artifact Registry -------------------------------------------------------

resource "google_artifact_registry_repository" "repo" {
  project       = var.project_id
  location      = var.region
  repository_id = "sonar-remediation-repo"
  format        = "DOCKER"
  labels        = { environment = var.environment }

  # Every deploy pushes a new SHA-tagged image and nothing ever pruned the
  # old ones -- confirmed the hard way: 26 images accumulated in sandbox
  # alone within a few days of active work. GCP has no hard storage quota
  # the way Azure Container Registry's Basic tier does, but this is still
  # unbounded, billed storage growth for images nothing will ever deploy
  # again. GCP evaluates cleanup_policies automatically (roughly daily),
  # no apply/workflow needed to keep it enforced going forward.
  #
  # KEEP always wins over DELETE for a version matching both -- so the 10
  # most recent versions of EITHER image (dashboard or agent) are never
  # touched by the age-based policy below, regardless of how old they get,
  # which is what keeps a rollback target available even after a long gap
  # between deploys.
  cleanup_policies {
    id     = "keep-most-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-older-than-30-days"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }
}

# --- Dashboard service account + IAM -----------------------------------------
# Exactly the 4 roles bound live today (roles/secretmanager.admin
# supersedes secretAccessor, but both are kept -- see the security-review
# fix earlier this session that added .admin without removing the
# original .secretAccessor grant; matched here rather than "cleaning it
# up" so prod's import is a true no-op).

resource "google_service_account" "dashboard_sa" {
  project      = var.project_id
  account_id   = "sonar-dashboard-sa"
  display_name = "Sonar Dashboard backend"
}

resource "google_project_iam_member" "dashboard_sa_roles" {
  for_each = toset([
    "roles/datastore.user",
    "roles/run.developer",
    "roles/secretmanager.admin",
    "roles/secretmanager.secretAccessor",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.dashboard_sa.email}"
}

# --- Agent service account -----------------------------------------------
# The Cloud Run Jobs never had an explicit service_account set, so they'd
# silently run as the default compute SA. That was invisible on prod
# (created before mid-2024, when GCP still auto-granted that SA a broad
# Editor role at project creation) but sandbox -- created fresh during
# this session -- got a near-empty default SA, and the very first real
# job update failed reading google-api-key: "Permission denied ... must
# be granted Secret Manager Secret Accessor". Rather than lean on that
# legacy auto-grant (itself an anti-pattern GCP has since dropped), giving
# the jobs their own least-privilege identity is the correct fix, not a
# workaround -- mirrors the agent/dashboard identity split already used on
# the Azure side.
resource "google_service_account" "agent_sa" {
  project      = var.project_id
  account_id   = "sonar-agent-sa"
  display_name = "Sonar agent Cloud Run Jobs"
}

resource "google_project_iam_member" "agent_sa_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# --- Firestore -----------------------------------------------------------

resource "google_firestore_database" "default" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
}

# --- Secrets ---------------------------------------------------------------
# manage_session_secret_version is false for prod (imported secret; a real
# signing key already backs every live session) and true for sandbox
# (fresh secret, nothing depends on it yet). google-api-key is the same
# "unused fallback" the Cloud Run Jobs' own baked-in env still references
# (see cloud_run.py's docstring -- the dashboard's per-execution override
# is what every real run actually uses) but the resource must still exist
# for the job definitions below to apply cleanly.

resource "google_secret_manager_secret" "dashboard_session_secret" {
  project   = var.project_id
  secret_id = "dashboard-session-secret"
  labels    = { environment = var.environment }
  replication {
    auto {}
  }
}

resource "random_password" "session_secret" {
  count   = var.manage_session_secret_version ? 1 : 0
  length  = 64
  special = false
}

resource "google_secret_manager_secret_version" "dashboard_session_secret" {
  count       = var.manage_session_secret_version ? 1 : 0
  secret      = google_secret_manager_secret.dashboard_session_secret.id
  secret_data = random_password.session_secret[0].result
}

resource "google_secret_manager_secret" "google_api_key" {
  project   = var.project_id
  secret_id = "google-api-key"
  labels    = { environment = var.environment }
  replication {
    auto {}
  }
}

resource "random_password" "google_api_key_placeholder" {
  count   = var.manage_session_secret_version ? 1 : 0
  length  = 32
  special = false
}

resource "google_secret_manager_secret_version" "google_api_key" {
  count  = var.manage_session_secret_version ? 1 : 0
  secret = google_secret_manager_secret.google_api_key.id
  # Placeholder only -- this fallback path is never actually read by a
  # real run (the dashboard always overrides it per-execution). A sandbox
  # environment that DOES want to exercise this fallback path directly
  # should set a real version by hand afterward, same as prod's real key
  # was set outside Tofu originally.
  secret_data = var.manage_session_secret_version ? random_password.google_api_key_placeholder[0].result : null
  lifecycle {
    ignore_changes = [secret_data]
  }
}

# --- Cloud Run Jobs (the three agents) ---------------------------------------

resource "google_cloud_run_v2_job" "agent" {
  for_each            = toset(["techdebt", "coverage", "duplicate"])
  project             = var.project_id
  name                = "sonar-remediation-${each.key}-job"
  location            = var.region
  labels              = { environment = var.environment }
  deletion_protection = var.deletion_protection

  template {
    task_count = 1
    template {
      max_retries     = 0
      timeout         = "3600s"
      service_account = google_service_account.agent_sa.email
      containers {
        image = var.agent_image
        resources {
          limits = {
            cpu    = "2"
            memory = "2Gi"
          }
        }
        env {
          name  = "AGENT_TYPE"
          value = each.key
        }
        # Baked-in fallback values -- every real run overrides these
        # per-execution from runs.py's create_run(); see cloud_run.py's
        # module docstring. Kept only so the job has a valid definition.
        env {
          name  = "SONAR_BASE_URL"
          value = "http://placeholder:9000"
        }
        env {
          name  = "GITHUB_REPO"
          value = "placeholder/placeholder"
        }
        env {
          name  = "LANGUAGE"
          value = "java"
        }
        env {
          name  = "CE_EDITION"
          value = "true"
        }
        env {
          name = "GOOGLE_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.google_api_key.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
}

# --- Cloud Run Service (dashboard) -------------------------------------------

resource "google_cloud_run_v2_service" "dashboard" {
  project             = var.project_id
  name                = "sonar-dashboard"
  location            = var.region
  labels              = { environment = var.environment }
  deletion_protection = var.deletion_protection

  scaling {
    min_instance_count = 0
  }

  template {
    service_account = google_service_account.dashboard_sa.email
    containers {
      image = var.dashboard_image
      ports {
        name           = "http1"
        container_port = 8080
      }
      resources {
        cpu_idle          = true
        startup_cpu_boost = true
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "COOKIE_SECURE"
        value = "true"
      }
      env {
        name = "SESSION_SECRET_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.dashboard_session_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }
}

# Public HTTPS access -- the app's own login/session system is the access
# control (see main.py's SecurityHeadersMiddleware docstring and the OWASP
# review earlier this session), same as any normal internet-facing web app.
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dashboard.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- GitHub Actions OIDC (Workload Identity Federation) ----------------------
# Matches docs/GCP_DEPLOYMENT.md Section 4's design exactly -- that doc was
# accurate, it just documented steps nobody had actually run yet (confirmed
# live: no WIF pool existed on GCP before this module). Keyless: GitHub's
# own OIDC token is exchanged for short-lived credentials, no static
# service-account key ever stored in GitHub.

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-pool-${var.environment}"
  display_name              = "GitHub Actions (${var.environment})"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  display_name                       = "GitHub Actions"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == '${var.github_repo}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "github_deployer" {
  project      = var.project_id
  account_id   = "github-deployer"
  display_name = "GitHub Actions deployer (${var.environment})"
}

resource "google_project_iam_member" "github_deployer_roles" {
  # This SA runs `tofu apply` for the whole GCP module, not just "push an
  # image and update a running service" -- the first real CI run proved
  # run.developer/artifactregistry.writer/iam.serviceAccountUser weren't
  # enough: Tofu also needs to READ (and on drift, write) the project's
  # IAM bindings, the Firestore database, the Secret Manager secrets, and
  # the WIF pool itself, none of which those three roles cover. Scoped to
  # what the module's resource types actually need, short of project
  # Owner -- projectIamAdmin is still broad (can grant/revoke any role to
  # any principal on this project), but it's the one role this can't
  # avoid needing, since the module itself manages project IAM bindings.
  for_each = toset([
    "roles/run.admin",
    "roles/artifactregistry.admin",
    "roles/iam.serviceAccountAdmin",
    # serviceAccountAdmin lets it create/manage SAs, but NOT attach one to
    # a resource (that's the separate actAs permission) -- confirmed the
    # hard way: updating a Cloud Run Job/Service that references
    # sonar-agent-sa/dashboard_sa 403'd with "Permission
    # iam.serviceaccounts.actAs denied" once this role was dropped in
    # favor of serviceAccountAdmin.
    "roles/iam.serviceAccountUser",
    "roles/resourcemanager.projectIamAdmin",
    "roles/datastore.owner",
    "roles/secretmanager.admin",
    "roles/iam.workloadIdentityPoolAdmin",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_service_account_iam_member" "github_deployer_wif" {
  service_account_id = google_service_account.github_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.github.workload_identity_pool_id}/attribute.repository/${var.github_repo}"
}
