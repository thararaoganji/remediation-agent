# Azure Infrastructure Guide (OpenTofu)

This document covers everything required to provision, manage, and tear down the Remediation Agent infrastructure on Microsoft Azure.

---

## 1. Overview & Architecture

The Azure infrastructure runs in dedicated OpenTofu roots per environment (`infra/environments/<environment>/azure`), completely isolated from GCP.

### Environment Names & Regions:
- **`sb` (New Clean Environment in Central India)**:
  - **Primary Region**: `centralindia`
  - **Resource Naming Convention** (`<prefix>-<environment>-<name>`):
    - **Resource Group**: `rg-sb-sonar-remediation`
    - **Container Registry (ACR)**: `acrsbsonarremediation` (Basic tier, alphanumeric only)
    - **Key Vault**: `kv-sb-sonar-remed`
    - **Cosmos DB**: `cosmos-sb-sonar-remediation` (Serverless SQL API)
    - **Log Analytics**: `log-sb-sonar-remediation`
    - **Container App Environment**: `cae-sb-sonar-remediation`
    - **Managed Identities**: `id-sb-sonar-dashboard` and `id-sb-sonar-agent`
    - **Container App (Dashboard)**: `sonar-dashboard`
    - **Container Apps Jobs (Agents)**: `sonar-remediation-techdebt-job`, `sonar-remediation-coverage-job`, `sonar-remediation-duplicate-job`
- **`sandbox` (Legacy East US Environment)**:
  - Runs in `eastus` (`sonar-remediation-sandbox-rg`, `sonarremediationsandboxacr`, etc.) alongside `sb` with zero interference.
- **`prod`**:
  - Reserved for production deployments.

---

## 2. State Backend

The OpenTofu state for Azure is stored in Azure Blob Storage in `centralindia` — completely independent of GCP:

- **Subscription**: `gms2azure` (`69cf5bbc-a9a8-41e2-861c-b3e6f78ad335`)
- **Resource Group**: `tofu-state-rg` (`centralindia`)
- **Storage Account**: `sonarremediationtfstate`
- **Container**: `tfstate`
- **State Blobs**:
  - `sb.tfstate` (`sb` environment in Central India)
  - `sandbox.tfstate` (`sandbox` environment in East US)
  - `prod.tfstate` (`prod` environment)
- **Auth**: Keyless RBAC via `Storage Blob Data Contributor` + OIDC token in CI (`use_azuread_auth = true`, `use_oidc = true`).

To initialize manually for `sb`:
```bash
cd infra/environments/sb/azure
tofu init \
  -backend-config="resource_group_name=tofu-state-rg" \
  -backend-config="storage_account_name=sonarremediationtfstate" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=sb.tfstate"
```

---

## 3. Greenfield Setup from Scratch (`centralindia` with `sb`)

You can set up all Azure resources from scratch either using the automated bootstrap script or manually step-by-step.

### Option A: Automated Script (Recommended)

From the project root:

**PowerShell (Windows):**
```powershell
.\infra\scripts\bootstrap-azure.ps1 -Environment sb -Location centralindia
```

**Bash (Linux / macOS / WSL / Git Bash):**
```bash
./infra/scripts/bootstrap-azure.sh sb centralindia
```

The script automatically:
1. Registers Azure resource providers (`Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.DocumentDB`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`).
2. Connects to `tofu-state-rg` / `sonarremediationtfstate` in `centralindia`.
3. Creates the Entra ID application (`sonar-remediation-github-deployer`), Service Principal, and GitHub OIDC federated credential for `sb`.
4. Grants the deployer SP `Storage Blob Data Contributor` on `sonarremediationtfstate`.
5. Provisions base infrastructure (`rg-sonar-remediation-sb`, `acrsonarremediationsb`, `kv-sonar-remed-sb`, `cosmos-sonar-remediation-sb`, and Managed Identities).
6. Builds and pushes initial bootstrap container images to ACR (`acrsonarremediationsb.azurecr.io/sonar-dashboard:latest` and `sonar-remediation-agent:latest`).
7. Grants data-plane RBAC permissions (Cosmos DB SQL Data Contributor, Key Vault Secrets Officer/User, Container Apps Operator).
8. Runs the final `tofu apply` to spin up the Container App Environment, dashboard, and agent jobs.
9. Prints the exact GitHub Environment Variables to configure in your repo.

---

### Option B: Step-by-Step Manual Bootstrap

#### Step 1: Register Resource Providers
```bash
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.ContainerRegistry
az provider register --namespace Microsoft.DocumentDB
az provider register --namespace Microsoft.KeyVault
```

#### Step 2: Ensure State Storage Exists in `centralindia`
```bash
az group create --name tofu-state-rg --location centralindia

az storage account create \
  --name sonarremediationtfstate \
  --resource-group tofu-state-rg \
  --location centralindia \
  --sku Standard_LRS \
  --kind StorageV2 \
  --allow-blob-public-access false

az storage container create \
  --name tfstate \
  --account-name sonarremediationtfstate \
  --auth-mode login
```

#### Step 3: Grant Deployer SP Access to State Storage
```bash
SUB_ID=$(az account show --query id -o tsv)
SP_OBJ_ID=$(az ad sp list --display-name "sonar-remediation-github-deployer" --query "[0].id" -o tsv)

az role assignment create \
  --assignee-object-id "$SP_OBJ_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/${SUB_ID}/resourceGroups/tofu-state-rg/providers/Microsoft.Storage/storageAccounts/sonarremediationtfstate"
```

#### Step 4: Run Initial OpenTofu Apply
```bash
cd infra/environments/sb/azure
tofu init \
  -backend-config="resource_group_name=tofu-state-rg" \
  -backend-config="storage_account_name=sonarremediationtfstate" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=sb.tfstate"

tofu apply \
  -target=module.azure.azurerm_resource_group.main \
  -target=module.azure.azurerm_container_registry.acr \
  -target=module.azure.azurerm_key_vault.main \
  -target=module.azure.azurerm_cosmosdb_account.main \
  -target=module.azure.azurerm_user_assigned_identity.agent \
  -target=module.azure.azurerm_user_assigned_identity.dashboard
```

#### Step 5: Push Initial Images to ACR
```bash
ACR_NAME="acrsbsonarremediation"
az acr login --name "$ACR_NAME"

# Build & push dashboard
docker build -f dashboard/Dockerfile -t "${ACR_NAME}.azurecr.io/sonar-dashboard:latest" .
docker push "${ACR_NAME}.azurecr.io/sonar-dashboard:latest"

# Build & push agent
docker build -f Dockerfile -t "${ACR_NAME}.azurecr.io/sonar-remediation-agent:latest" .
docker push "${ACR_NAME}.azurecr.io/sonar-remediation-agent:latest"
```

#### Step 6: Grant RBAC Roles
```bash
RG="rg-sb-sonar-remediation"
ACR_ID=$(az acr show --name "$ACR_NAME" --query id -o tsv)
DASHBOARD_ID=$(az identity show --name "id-sb-sonar-dashboard" --resource-group "$RG" --query principalId -o tsv)
AGENT_ID=$(az identity show --name "id-sb-sonar-agent" --resource-group "$RG" --query principalId -o tsv)
COSMOS_NAME=$(az cosmosdb list --resource-group "$RG" --query "[0].name" -o tsv)

# AcrPull for both identities
az role assignment create --assignee-object-id "$DASHBOARD_ID" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID"
az role assignment create --assignee-object-id "$AGENT_ID" --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID"

# Key Vault Secrets roles
KV_ID=$(az keyvault show --name "kv-sb-sonar-remed" --resource-group "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$DASHBOARD_ID" --assignee-principal-type ServicePrincipal --role "Key Vault Secrets Officer" --scope "$KV_ID"
az role assignment create --assignee-object-id "$AGENT_ID" --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User" --scope "$KV_ID"
az role assignment create --assignee-object-id "$SP_OBJ_ID" --assignee-principal-type ServicePrincipal --role "Key Vault Administrator" --scope "$KV_ID"

# Container Apps permissions for dashboard
RG_ID=$(az group show --name "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$DASHBOARD_ID" --assignee-principal-type ServicePrincipal --role "Container Apps Contributor" --scope "$RG_ID"
az role assignment create --assignee-object-id "$DASHBOARD_ID" --assignee-principal-type ServicePrincipal --role "Container Apps Jobs Operator" --scope "$RG_ID"

# Cosmos DB Built-in Data Contributor (data-plane RBAC)
ROLE_DEF_ID="00000000-0000-0000-0000-000000000002"
az cosmosdb sql role assignment create --account-name "$COSMOS_NAME" --resource-group "$RG" --role-definition-id "/subscriptions/${SUB_ID}/resourceGroups/${RG}/providers/Microsoft.DocumentDB/databaseAccounts/${COSMOS_NAME}/sqlRoleDefinitions/${ROLE_DEF_ID}" --principal-id "$DASHBOARD_ID" --scope "/"
az cosmosdb sql role assignment create --account-name "$COSMOS_NAME" --resource-group "$RG" --role-definition-id "/subscriptions/${SUB_ID}/resourceGroups/${RG}/providers/Microsoft.DocumentDB/databaseAccounts/${COSMOS_NAME}/sqlRoleDefinitions/${ROLE_DEF_ID}" --principal-id "$AGENT_ID" --scope "/"

# Deployer permissions on Resource Group and ACR
az role assignment create --assignee-object-id "$SP_OBJ_ID" --assignee-principal-type ServicePrincipal --role Contributor --scope "$RG_ID"
az role assignment create --assignee-object-id "$SP_OBJ_ID" --assignee-principal-type ServicePrincipal --role AcrPush --scope "$ACR_ID"
```

#### Step 7: Complete OpenTofu Apply
```bash
tofu apply -auto-approve
```

---

## 4. GitHub Actions Configuration

Configure the following **Variables** under **Settings → Environments → sb**:

| Variable | Value for `sb` (`centralindia`) |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | `69cf5bbc-a9a8-41e2-861c-b3e6f78ad335` |
| `AZURE_TENANT_ID` | Your Azure Entra ID Tenant GUID |
| `AZURE_CLIENT_ID` | Application (client) ID of `sonar-remediation-github-deployer` |
| `AZURE_RESOURCE_GROUP` | `rg-sb-sonar-remediation` |
| `AZURE_ACR_NAME` | `acrsbsonarremediation` |
| `AZURE_LOCATION` | `centralindia` |

**Repository-level Variables** (shared across all environments):
- `TOFU_STATE_AZURE_RESOURCE_GROUP`: `tofu-state-rg`
- `TOFU_STATE_AZURE_STORAGE_ACCOUNT`: `sonarremediationtfstate`
- `TOFU_STATE_AZURE_CONTAINER`: `tfstate`

---

## 5. Ongoing Operations & Pipelines

### Deploying the Dashboard
- **Workflow**: `.github/workflows/deploy-dashboard-azure.yml`
- **Trigger**: Manual (`workflow_dispatch`), select `sb`.
- Builds and pushes `sonar-dashboard:<SHA>`, resolves the live agent image tag from Azure Container App Jobs, and applies OpenTofu. Only the dashboard Container App is updated.

### Deploying the Agents
- **Workflow**: `.github/workflows/deploy-agent-azure.yml`
- **Trigger**: Manual (`workflow_dispatch`), select `sb`.
- Builds and pushes `sonar-remediation-agent:<SHA>`, resolves the live dashboard image tag, and applies OpenTofu. Only the 3 agent Container Apps Jobs are updated.

### Registry Cleanup (ACR)
- **Workflow**: `.github/workflows/cleanup-azure.yml`
- **Trigger**: Manual (`workflow_dispatch`), select `sb`.

### Tearing Down `sb` Environment
- **Workflow**: `.github/workflows/destroy-azure.yml`
- **Trigger**: Manual (`workflow_dispatch`), select `sb`, type `destroy-sb`.
- Only destroys resources in `rg-sb-sonar-remediation`. The East US `sandbox` resources and state remain completely untouched.
