# Infrastructure (OpenTofu)

This directory contains the OpenTofu (Terraform-compatible) infrastructure configuration for the Remediation Agent platform.

Two clouds, **Azure** and **GCP**, are supported across two environments: `sandbox` and `prod`. Each cloud operates as a **completely independent OpenTofu root** with its own state storage, credentials, and CI/CD pipelines. A deployment or failure in one cloud has zero effect on the other.

---

## Dedicated Cloud Documentation

Comprehensive guides, initial bootstrap steps, and pipeline details are maintained in separate dedicated files:

- 📘 **[Azure Infrastructure Guide](AZURE.md)** — Architecture, state backend (`tofu-state-rg` / `sonarremediationtfstate`), automated setup scripts for `centralindia`, Entra ID OIDC setup, RBAC role grants, Container Apps, and CI/CD pipelines.
- 📗 **[GCP Infrastructure Guide](GCP.md)** — Architecture, GCS state backend, Workload Identity Federation (WIF), Cloud Run services & jobs, Artifact Registry automated cleanup, and CI/CD pipelines.

---

## Directory Structure

```
infra/
├── AZURE.md                          # Dedicated Azure infrastructure guide & setup
├── GCP.md                            # Dedicated GCP infrastructure guide & setup
├── README.md                         # This architecture overview
├── scripts/
│   ├── bootstrap-azure.ps1           # Automated PowerShell setup script for Azure (centralindia)
│   └── bootstrap-azure.sh            # Automated Bash setup script for Azure (centralindia)
├── environments/
│   ├── sandbox/
│   │   ├── azure/                    # Azure sandbox root (state: sonarremediationtfstate/sandbox.tfstate)
│   │   └── gcp/                      # GCP sandbox root (state: gs://sonar-remediation-tofu-state/sandbox/gcp)
│   └── prod/
│       ├── azure/                    # Azure prod root (state: sonarremediationtfstate/prod.tfstate)
│       └── gcp/                      # GCP prod root (state: gs://sonar-remediation-tofu-state/prod/gcp)
└── modules/
    ├── azure/                        # Shared Azure module (RG, ACR, Cosmos DB, Key Vault, Container Apps)
    └── gcp/                          # Shared GCP module (Artifact Registry, Firestore, Secret Manager, Cloud Run)
```

---

## Core Design Principles

1. **Root Isolation**:
   - GCP roots use GCS (`gs://sonar-remediation-tofu-state`).
   - Azure roots use Azure Storage Account (`tofu-state-rg` / `sonarremediationtfstate`).
   - Neither cloud touches the other's state, preventing cross-cloud locks or stale plan races.

2. **Decoupled Dashboard & Agent Pipelines**:
   - The **Dashboard** (web UI + REST API) and **Agents** (batch remediation jobs) are independent entities.
   - Deploys run via separate GitHub Actions workflows:
     - Azure: `deploy-dashboard-azure.yml` & `deploy-agent-azure.yml`
     - GCP: `deploy-dashboard-gcp.yml` & `deploy-agent-gcp.yml`
   - Each workflow builds only its component, queries live cloud resources to resolve the other component's image tag, and applies OpenTofu with zero side-effects.

3. **Scale-to-Zero Cost Model**:
   - Both Azure Container Apps and GCP Cloud Run scale to zero replicas when idle.
   - Cosmos DB Serverless and Cloud Firestore charge per operation, incurring negligible cost when inactive.

4. **Security & Keyless Authentication**:
   - GitHub Actions workflows use OIDC / Workload Identity Federation with short-lived tokens.
   - No long-lived service account keys or Azure client secrets are stored in GitHub Secrets.
