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

## Registry cleanup

Every deploy pushes a new SHA-tagged image and nothing deletes the old
ones on its own. Confirmed the hard way (2026-09-30): sandbox's Azure
Container Registry -- Basic tier, chosen for cost -- hit 86% of its hard
10 GiB storage quota after a few days of active deploys, since Basic has
no native retention policy at all (that's a Premium-only ACR feature).
GCP's Artifact Registry has no such hard quota, but was still carrying 26
unpruned images in the same environment.

- **GCP**: `google_artifact_registry_repository.repo`'s own
  `cleanup_policies` (`infra/modules/gcp/main.tf`) keep the 10 most recent
  versions of each image plus whatever's currently deployed, and delete
  anything else older than 30 days -- enforced by GCP automatically
  (roughly daily), no workflow needed once applied. `.github/workflows/
  cleanup-gcp.yml` (manual trigger) exists for when immediate action beats
  waiting for that sweep.
- **Azure**: no native equivalent exists on Basic tier, so
  `.github/workflows/cleanup-azure.yml` (manual trigger,
  `.github/actions/cleanup-azure-acr`) **is** the retention policy here --
  it needs to actually be run periodically, not just exist. Both
  workflows always read the live currently-deployed tag from the
  dashboard/jobs before deciding what to delete, so a real rollback target
  is never at risk even if it's older than the keep-count window.

Run either from the Actions tab, pick `sandbox`/`prod`/`both` and how many
recent versions to keep (defaults: 10 for GCP, 5 for Azure). First real
run against sandbox (2026-09-30) took Azure from 86% to ~30% of quota (24
images deleted) and GCP from 26 to 22 images (4 deleted -- most of its 26
were already within the keep-10 window).

## Tearing sandbox down and spinning it back up

Since everything already scales to zero (both dashboards, every agent
job), the real idle cost here is already small -- see the architecture
doc's cost estimate. Tearing sandbox down entirely is more about not
leaving real resources running unused than about meaningful savings, and
it's deliberately **sandbox-only**: `destroy-gcp.yml`/`destroy-azure.yml`
don't even offer `prod` as a choice (prod carries real imported
infrastructure -- see "GCP prod's import" below -- a typo'd environment
pick here has no undo). Both also require typing `destroy-sandbox` into
the trigger form itself, a second gate on top of the manual trigger,
since this is meaningfully more destructive than a deploy or a registry
cleanup.

Both destroy workflows exclude the GitHub OIDC trust objects
(`tofu-destroy-gcp`/`tofu-destroy-azure`'s own `-exclude` flags) so the
exact same workflow credentials can still authenticate afterward -- but
**what that authentication is actually worth to rebuild with differs by
cloud**, confirmed by reading each module's real resource graph (not
assumed):

- **GCP**: `github_deployer`'s permissions
  (`google_project_iam_member.github_deployer_roles`) are granted at the
  **project** level, not scoped to anything the destroy removes -- they
  survive completely untouched. Run `destroy-gcp.yml`, then
  `deploy-gcp.yml` -- no manual step in between, full automation both
  ways.
- **Azure**: `github_deployer`'s only grants (Contributor on the resource
  group, AcrPush on the registry) are scoped to resources the destroy
  *does* remove -- including the resource group itself. After
  `destroy-azure.yml`, `github_deployer` can still log in but can do
  nothing else at all, not even create a replacement resource group.
  `deploy-azure.yml` will fail on its very first `tofu apply` until
  someone with real subscription permissions re-runs the resource-group +
  Contributor-grant part of "Bootstrap order" (step 3/4) above -- the same
  ABAC-constrained-Owner limitation the original bootstrap hit, not a bug
  in either workflow.

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
