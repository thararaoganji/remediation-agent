# GCP Infrastructure Guide (OpenTofu)

This document covers everything required to provision, manage, and tear down the Remediation Agent infrastructure on Google Cloud Platform.

---

## 1. Overview & Architecture

The GCP infrastructure runs in a dedicated OpenTofu root (`infra/environments/<environment>/gcp`), completely isolated from Azure. It deploys the following resources:

- **Primary Region**: `us-central1`
- **GCP Project**: `sonar-remediation-sandbox` (sandbox) and `aiproject-495122` (prod)
- **Artifact Registry Repository**: `sonar-remediation-repo` (Docker format) with automated retention policies
- **Firestore**: Native mode database `(default)` in `us-central1` for dashboard persistence
- **Secret Manager**:
  - `dashboard-session-secret`: Session signing key (auto-generated in sandbox; imported in prod)
  - `google-api-key`: API key container for Gemini access
- **Cloud Run (Service)**:
  - `sonar-dashboard`: Long-running HTTP service for the FastAPI + React dashboard (scales to 0 when idle)
- **Cloud Run (Jobs)**:
  - `sonar-remediation-techdebt-job`: On-demand job for the Tech Debt agent
  - `sonar-remediation-coverage-job`: On-demand job for the Coverage agent
  - `sonar-remediation-duplicate-job`: On-demand job for the Duplication agent
- **Service Accounts & IAM**:
  - `sonar-dashboard-sa`: Identity for the dashboard service (`roles/datastore.user`, `roles/run.developer`, `roles/secretmanager.admin`, `roles/secretmanager.secretAccessor`)
  - `sonar-agent-sa`: Least-privilege identity for agent jobs (`roles/secretmanager.secretAccessor`)
  - `github-deployer`: CI identity impersonated via Workload Identity Federation

---

## 2. State Backend

The OpenTofu state for GCP is stored in Google Cloud Storage — completely independent of Azure:

- **Bucket**: `gs://sonar-remediation-tofu-state` (versioning enabled, hosted in `aiproject-495122`)
- **State Objects**: `sandbox/gcp/default.tfstate` and `prod/gcp/default.tfstate`
- **Auth**: Native Google Cloud credentials (local `gcloud auth application-default login` or Workload Identity Federation in CI)

To initialize manually:
```bash
cd infra/environments/sandbox/gcp  # or prod/gcp
tofu init \
  -backend-config="bucket=sonar-remediation-tofu-state" \
  -backend-config="prefix=sandbox/gcp"
```

---

## 3. Greenfield Setup from Scratch

To set up a fresh GCP environment (e.g. sandbox):

### Step 1: Create GCP Project & Enable APIs
```bash
PROJECT_ID="sonar-remediation-sandbox"
REGION="us-central1"

gcloud projects create "$PROJECT_ID"
# Link project to your billing account via console or:
# gcloud billing projects link "$PROJECT_ID" --billing-account=BILLING_ACCOUNT_ID

gcloud config set project "$PROJECT_ID"

gcloud services enable \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  sts.googleapis.com
```

### Step 2: Grant CI Access to the State Bucket
Every environment's `github-deployer` service account needs object admin rights on the shared state bucket:
```bash
gcloud storage buckets add-iam-policy-binding gs://sonar-remediation-tofu-state \
  --member="serviceAccount:github-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

### Step 3: Setup Workload Identity Federation (WIF)
```bash
REPO="OWNER/REPO" # e.g. acme/remediation-agent
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# Create WIF Pool
gcloud iam workload-identity-pools create "github-pool-sandbox" \
  --project="$PROJECT_ID" \
  --location="global" \
  --display-name="GitHub Actions Pool (sandbox)"

# Create WIF Provider
gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --project="$PROJECT_ID" \
  --location="global" \
  --workload-identity-pool="github-pool-sandbox" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO}'"

# Create Deployer Service Account
gcloud iam service-accounts create github-deployer \
  --project="$PROJECT_ID" \
  --display-name="GitHub Actions Deployer"

SA="github-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# Grant project roles
for role in roles/run.developer roles/artifactregistry.writer roles/iam.serviceAccountUser roles/resourcemanager.projectIamAdmin roles/secretmanager.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" --role="$role"
done

# Allow GitHub Actions to impersonate SA
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool-sandbox/attribute.repository/${REPO}"
```

### Step 4: Initial OpenTofu Apply
```bash
cd infra/environments/sandbox/gcp
tofu init \
  -backend-config="bucket=sonar-remediation-tofu-state" \
  -backend-config="prefix=sandbox/gcp"

tofu apply -auto-approve
```

---

## 4. GitHub Actions Configuration

Configure the following **Variables** under **Settings → Environments → sandbox** (and `prod`):

| Variable | Value (Example for sandbox) |
|---|---|
| `GCP_PROJECT_ID` | `sonar-remediation-sandbox` |
| `GCP_REGION` | `us-central1` |
| `GCP_WIF_PROVIDER` | `projects/<NUM>/locations/global/workloadIdentityPools/github-pool-sandbox/providers/github-provider` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `github-deployer@sonar-remediation-sandbox.iam.gserviceaccount.com` |

**Repository-level Variable**:
- `TOFU_STATE_BUCKET`: `sonar-remediation-tofu-state`

---

## 5. Ongoing Operations & Pipelines

### Deploying the Dashboard
- **Workflow**: `.github/workflows/deploy-dashboard-gcp.yml`
- **Trigger**: Manual (`workflow_dispatch`), select `sandbox` or `prod`.
- Builds and pushes `sonar-dashboard:<SHA>`, resolves the live agent image tag from Cloud Run Jobs, and applies OpenTofu. Only the Cloud Run dashboard service is updated.

### Deploying the Agents
- **Workflow**: `.github/workflows/deploy-agent-gcp.yml`
- **Trigger**: Manual (`workflow_dispatch`), select `sandbox` or `prod`.
- Builds and pushes `sonar-remediation-agent:<SHA>`, resolves the live dashboard image tag, and applies OpenTofu. Only the 3 Cloud Run agent jobs are updated.

### Automated Registry Retention & Cleanup
- **Automatic Enforcement**: The Artifact Registry repository defines cleanup policies in `infra/modules/gcp/main.tf`:
  - `keep-most-recent`: Retains the 10 most recent versions of each image tag.
  - `delete-older-than-30-days`: Purges images older than 30 days.
  - GCP evaluates these policies daily in the background.
- **On-Demand Cleanup**: `.github/workflows/cleanup-gcp.yml` allows immediate manual purging of old SHA-tagged digests while preserving active deployments.

### Tearing Down Sandbox
- **Workflow**: `.github/workflows/destroy-gcp.yml`
- **Trigger**: Manual (`workflow_dispatch`), requires typing `"destroy-sandbox"`.
- Destroys Cloud Run service & jobs, Firestore database, Secret Manager secrets, and Artifact Registry.
- **Excluded**: Workload Identity Pool, WIF Provider, `github-deployer` service account, and project IAM roles.
- **Rebuilding after destroy**: Because deployer IAM roles are granted at the GCP project level, they survive destroy completely intact. You can recreate the environment with zero manual steps.

---

## 6. GCP Prod's Import History

Prod's GCP resources predate this repo's OpenTofu setup (they were created manually or via `gcloud` during earlier work). They were imported resource-by-resource (`tofu import`) rather than recreated, ensuring `tofu apply` never interrupted the live dashboard.

After import, `tofu plan` displays only two harmless diffs:
1. `environment = "prod"` label (intentional).
2. `client`/`client_version` fields nulling out (read-only provenance metadata showing which tool last touched the resource, cleared under OpenTofu management).
No functional attribute of any imported resource differs from what is live.
