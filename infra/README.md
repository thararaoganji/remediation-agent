# Infrastructure (OpenTofu)

Two environments, `sandbox` and `prod`, each deploying the same shapes to
both GCP and Azure:

```
infra/
  modules/
    gcp/            # one environment's worth of GCP resources
    azure/          # one environment's worth of Azure resources
  environments/
    sandbox/        # calls both modules with sandbox vars
    prod/           # calls both modules with prod vars (GCP side imported from
                     # infrastructure that predates this repo's Tofu setup)
```

## State backend

State lives in a single GCS bucket, one object per environment:

- Bucket: `gs://sonar-remediation-tofu-state` (versioning on, in `aiproject-495122`)
- Objects: `prod/default.tfstate`, `sandbox/default.tfstate` (the `-backend-config="prefix=..."` below is a folder-like prefix; OpenTofu names the object itself `default.tfstate` under it)

`providers.tf` in each environment leaves the bucket/prefix out of the
`backend "gcs" {}` block on purpose, so the same file works for local use and
CI without editing. Supply them via `-backend-config` at init time:

```bash
cd infra/environments/sandbox   # or prod
tofu init \
  -backend-config="bucket=sonar-remediation-tofu-state" \
  -backend-config="prefix=sandbox"   # or "prefix=prod"
```

CI does this automatically (see `.github/actions/tofu-deploy`).

## Bootstrap order (one-time, per environment)

These steps aren't part of the ordinary CI deploy loop -- they're what makes
CI possible in the first place:

1. **GCP project** (sandbox only -- prod's project already existed and was
   imported): `gcloud projects create <id>` + link it to a billing account,
   then enable `artifactregistry`, `run`, `firestore`, `secretmanager`,
   `iam`, `iamcredentials`, `cloudresourcemanager`, `sts`.
1a. **Grant that environment's `github-deployer` SA access to the state
   bucket**: the bucket lives in `aiproject-495122` (prod's project) but
   every environment's CI identity needs to read/write state in it,
   regardless of which project ITS resources live in -- a fresh
   environment's `github-deployer` SA has zero permissions there by
   default (confirmed the hard way: `tofu init` in CI failed with
   `storage.objects.list` denied on first use). Grant it directly on the
   bucket, not the whole project:
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://sonar-remediation-tofu-state \
     --member="serviceAccount:github-deployer@<project-id>.iam.gserviceaccount.com" \
     --role="roles/storage.objectAdmin"
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
   - Container Apps Contributor (resource group scope) -> the dashboard managed identity
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

Plus one repo-level Variable shared by both: `TOFU_STATE_BUCKET` =
`sonar-remediation-tofu-state`.

Deployment is manual-trigger-only for both environments -- there's no push
trigger at all, by design. Run `.github/workflows/deploy-gcp.yml` or
`deploy-azure.yml` from the Actions tab and pick `sandbox`, `prod`, or
`both`; each calls its cloud's own `reusable-deploy-{gcp,azure}.yml`,
which in turn calls the shared `tofu-deploy` composite action
(`.github/actions/tofu-deploy`) scoped to that one cloud's module.

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
