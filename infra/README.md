# Infrastructure (OpenTofu)

Two environments, `sandbox` and `prod`, each deploying the same shapes to
both GCP and Azure. Each cloud is a **fully independent** Tofu root, with
its own state file in its own cloud's storage -- a GCP deploy has zero
Azure touchpoint and vice versa, and the two can never race, lock against
each other, or fail because of a problem in the other cloud's
config/credentials.

```
infra/
  modules/
    gcp/                  # one environment's worth of GCP resources
    azure/                # one environment's worth of Azure resources
  environments/
    sandbox/
      gcp/                # sandbox's GCP root -- own state, own everything
      azure/               # sandbox's Azure root -- own state, own everything
    prod/
      gcp/                # prod's GCP root (imported from infrastructure
                           # that predates this repo's Tofu setup)
      azure/               # prod's Azure root (nothing applied here yet)
```

## State backends -- one per cloud, not shared

**GCP roots** (`infra/environments/{sandbox,prod}/gcp`) use a GCS bucket:

- Bucket: `gs://sonar-remediation-tofu-state` (versioning on, in `aiproject-495122`)
- Objects: `prod/gcp/default.tfstate`, `sandbox/gcp/default.tfstate`

**Azure roots** (`infra/environments/{sandbox,prod}/azure`) use an Azure
Storage Account -- deliberately *not* the same GCS bucket, so an Azure
deploy needs zero GCP credentials for anything, not even reading state:

- Resource group: `tofu-state-rg` (in the `gms2azure` subscription --
  never managed by the `azure` module itself, to avoid Tofu managing the
  very storage account its own state lives in)
- Storage account: `sonarremediationtfstate`
- Container: `tfstate`
- Blobs: `sandbox.tfstate`, `prod.tfstate`
- Auth is keyless: `use_azuread_auth = true` (RBAC via `Storage Blob Data
  Contributor` on the storage account, not a storage account key) +
  `use_oidc = true` (reuses the same federated-credential token
  `azure/login` already sets up in CI -- no separate secret needed just
  to unlock the backend)

Both backend blocks leave the bucket/account/container out of the
`backend {}` block on purpose, so the same file works for local use and CI
without editing. Supply them via `-backend-config` at init time:

```bash
# GCP
cd infra/environments/sandbox/gcp   # or prod/gcp
tofu init \
  -backend-config="bucket=sonar-remediation-tofu-state" \
  -backend-config="prefix=sandbox/gcp"   # or "prefix=prod/gcp"

# Azure
cd infra/environments/sandbox/azure   # or prod/azure
tofu init \
  -backend-config="resource_group_name=tofu-state-rg" \
  -backend-config="storage_account_name=sonarremediationtfstate" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=sandbox.tfstate"   # or "key=prod.tfstate"
```

CI does this automatically (see `.github/actions/tofu-deploy-gcp` and
`.github/actions/tofu-deploy-azure` -- two separate composite actions,
matching the two separate deploy pipelines).

## Bootstrap order (one-time, per environment)

These steps aren't part of the ordinary CI deploy loop -- they're what makes
CI possible in the first place:

1. **GCP project** (sandbox only -- prod's project already existed and was
   imported): `gcloud projects create <id>` + link it to a billing account,
   then enable `artifactregistry`, `run`, `firestore`, `secretmanager`,
   `iam`, `iamcredentials`, `cloudresourcemanager`, `sts`.
1a. **Grant that environment's GCP `github-deployer` SA access to the GCS
   state bucket**: the bucket lives in `aiproject-495122` (prod's
   project) but every environment's CI identity needs to read/write its
   own state in it, regardless of which project ITS resources live in --
   a fresh environment's `github-deployer` SA has zero permissions there
   by default (confirmed the hard way: `tofu init` in CI failed with
   `storage.objects.list` denied on first use). Grant it directly on the
   bucket, not the whole project:
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://sonar-remediation-tofu-state \
     --member="serviceAccount:github-deployer@<project-id>.iam.gserviceaccount.com" \
     --role="roles/storage.objectAdmin"
   ```
1b. **Grant that environment's Azure `github-deployer` SP access to the
   Azure state storage account** -- same reasoning as 1a, mirrored for
   Azure:
   ```bash
   az role assignment create \
     --assignee-object-id <github-deployer SP's object id> \
     --assignee-principal-type ServicePrincipal \
     --role "Storage Blob Data Contributor" \
     --scope "/subscriptions/<sub>/resourceGroups/tofu-state-rg/providers/Microsoft.Storage/storageAccounts/sonarremediationtfstate"
   ```
2. **Azure resource provider**: `az provider register --namespace Microsoft.App`
   (Container Apps) -- one-time per subscription, not per resource group.
3. **`tofu apply`** to create the base resources (resource group, ACR, Cosmos
   DB, Key Vault, managed identities, the GitHub OIDC app registration).
4. **RBAC that the applying identity itself can't grant**: this repo's Azure
   subscription owner account has a deliberately scoped-down `Owner` (an ABAC
   condition restricting which roles it can assign -- by design, not a bug).
   A genuine tenant Global Administrator must run these once per environment,
   after step 3's resources exist (they need the resource IDs):
   - Key Vault Administrator (to the identity that will run future applies)
   - Key Vault Secrets User -> the agent managed identity
   - Key Vault Secrets Officer -> the dashboard managed identity
   - Container Apps Contributor (resource group scope) -> the dashboard managed
     identity (covers `Microsoft.App/containerApps/*` only -- does NOT cover
     Jobs, see the next line)
   - Container Apps Jobs Operator (resource group scope) -> the dashboard
     managed identity (`Microsoft.App/jobs/*` read + start; Container Apps
     Jobs is a separate resource type from Container Apps and gets none of
     Contributor's actions above -- confirmed the hard way when starting a
     run 403'd with `AuthorizationFailed` on `Microsoft.App/jobs/read`
     despite Container Apps Contributor already being granted)
   - AcrPull (registry scope) -> both managed identities (naming an identity
     in a container/job's `registry { identity = ... }` block doesn't grant
     it anything by itself)
   - Cosmos DB Built-in Data Contributor (via `az cosmosdb sql role assignment
     create`, NOT `az role assignment create` -- Cosmos data-plane RBAC is a
     separate system from Azure RBAC) -> both managed identities
   - `az ad sp create --id <app-id>` + `az ad app federated-credential create`
     for the GitHub deployer app (needs an Entra directory role like
     Application Administrator, which the constrained account also lacks)
   - Contributor (resource group scope) + AcrPush (registry scope) -> the
     GitHub deployer service principal

   Then `tofu import` each of those into state (they're real resources now,
   just not Tofu-created), and confirm `tofu plan` shows zero drift before
   trusting it going forward.
5. **`tofu apply` again** to pick up everything that depended on step 4's
   grants (Key Vault secret write, Container Apps environment/jobs/app).

## GitHub Environments

Two GitHub Environments, `sandbox` and `prod`, each with its own copies of
these repo Variables (no Secrets needed anywhere -- both clouds authenticate
via OIDC/Workload Identity Federation, so none of these values are sensitive
on their own):

| Variable | Example |
|---|---|
| `GCP_PROJECT_ID` | `sonar-remediation-sandbox` |
| `GCP_REGION` | `us-central1` |
| `GCP_WIF_PROVIDER` | `projects/.../workloadIdentityPools/github-pool-sandbox/providers/github-provider` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `github-deployer@sonar-remediation-sandbox.iam.gserviceaccount.com` |
| `AZURE_SUBSCRIPTION_ID` | subscription GUID |
| `AZURE_TENANT_ID` | tenant GUID |
| `AZURE_CLIENT_ID` | the environment's GitHub-deployer app's client ID |
| `AZURE_RESOURCE_GROUP` | `sonar-remediation-sandbox-rg` |
| `AZURE_ACR_NAME` | `sonarremediationsandboxacr` |

Plus repo-level Variables shared by every environment (same state backend,
regardless of which environment is deploying):
`TOFU_STATE_BUCKET` = `sonar-remediation-tofu-state` (GCP),
`TOFU_STATE_AZURE_RESOURCE_GROUP` = `tofu-state-rg`,
`TOFU_STATE_AZURE_STORAGE_ACCOUNT` = `sonarremediationtfstate`,
`TOFU_STATE_AZURE_CONTAINER` = `tfstate`.

Deployment is manual-trigger-only for both environments -- there's no push
trigger at all, by design. Run `.github/workflows/deploy-gcp.yml` or
`deploy-azure.yml` from the Actions tab and pick `sandbox`, `prod`, or
`both`; each is a fully separate pipeline (`reusable-deploy-gcp.yml` /
`reusable-deploy-azure.yml` -> `tofu-deploy-gcp` / `tofu-deploy-azure`)
that never touches the other cloud.

`prod`'s Environment has a required-reviewers protection rule configured
in repo Settings -> Environments, but it's not actually enforced: GitHub
Environment protection rules need GitHub Pro/Team on a private repo, and
this repo is private. Until/unless that changes, the manual trigger
itself is the only real gate -- there's no second approval click on top
of it.

## GCP prod's import

Prod's GCP resources predate this repo's Tofu setup (they were created by
hand / via `gcloud` over the course of earlier work). They were imported
resource-by-resource (`tofu import`) rather than recreated, specifically so
`tofu apply` would never touch the live, already-running dashboard. After
import, `tofu plan` shows only two harmless categories of diff: the new
`environment = "prod"` label (intentional), and `client`/`client_version`
fields nulling out (read-only provenance metadata showing which tool last
touched the resource -- cosmetic, not settable in HCL, cleared once under
Tofu management). No functional attribute of any imported resource differs
from what's live.

Azure has no equivalent import -- nothing existed there before this repo's
Tofu setup, for either environment.

## History: splitting the combined state (2026-09-28)

Both environments originally had ONE Tofu root managing both clouds
together (`infra/environments/{sandbox,prod}/main.tf` called both
`module.gcp` and `module.azure`, sharing one state file). That made the
two clouds' CI pipelines genuinely coupled: both applied against the same
state, needing `-lock-timeout` so one would wait instead of erroring when
the other held the lock, and a plan saved before that wait could go
stale once the other cloud's apply changed the shared state ("Error:
Saved plan is stale"). Split into the fully independent structure above
so neither cloud's deploy can affect the other's, at all. Sandbox's
already-live combined state was split via `tofu state mv -state-out=...`
per module (a lossless relocation, not a data-altering operation) and
verified to produce a zero-diff `tofu plan` in both new roots before the
old combined state was retired.
