#!/usr/bin/env bash
# ==============================================================================
# Sonar Remediation Agent: Azure Bootstrap Script
#
# Sets up Azure infrastructure from scratch in centralindia (or specified region):
# 1. Registers required Azure resource providers.
# 2. Creates state storage (tofu-state-rg / sonarremediationtfstate).
# 3. Configures Entra ID GitHub OIDC deployer app and federated credential.
# 4. Runs OpenTofu init and targeted base apply (RG, ACR, Key Vault, Cosmos DB, Identities).
# 5. Builds and pushes initial Docker images to ACR.
# 6. Configures data-plane and management RBAC roles.
# 7. Runs full OpenTofu apply to spin up Container Apps environment, dashboard, and agent jobs.
# 8. Prints GitHub Actions environment variables for CI/CD.
# ==============================================================================

set -euo pipefail

ENVIRONMENT="${1:-sb}"
LOCATION="${2:-centralindia}"
SUBSCRIPTION_ID="${3:-}"
GITHUB_REPO="${4:-thararaoganji@176417932/remediation-agent@1322380487}"

echo "=== Sonar Remediation Agent: Azure Bootstrap ==="
echo "Environment: ${ENVIRONMENT}"
echo "Location:    ${LOCATION}"

# 1. Validate Prerequisites
echo ""
echo "[1/8] Checking prerequisites..."

AZ_BIN="az"
if command -v az >/dev/null 2>&1; then
  AZ_BIN="az"
elif command -v az.cmd >/dev/null 2>&1; then
  AZ_BIN="az.cmd"
elif [ -f "/c/work/azure-cli-2.72.0-x64/bin/az.cmd" ]; then
  AZ_BIN="/c/work/azure-cli-2.72.0-x64/bin/az.cmd"
elif [ -f "C:/work/azure-cli-2.72.0-x64/bin/az.cmd" ]; then
  AZ_BIN="C:/work/azure-cli-2.72.0-x64/bin/az.cmd"
else
  echo "Error: 'az' CLI is required. Ensure Azure CLI is installed and in PATH." >&2
  exit 1
fi

az() {
  "$AZ_BIN" "$@"
}
export -f az 2>/dev/null || true

command -v docker >/dev/null 2>&1 || { echo "Error: 'docker' is required." >&2; exit 1; }

TOFU_CMD="tofu"
if ! command -v tofu >/dev/null 2>&1 && ! command -v terraform >/dev/null 2>&1; then
  echo "OpenTofu/Terraform not found on PATH. Attempting automatic download..."
  mkdir -p .tools
  OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
  ARCH="amd64"
  VERSION="1.9.0"

  if [[ "$OS" == *"mingw"* || "$OS" == *"msys"* || "$OS" == *"cygwin"* ]]; then
    ZIP_URL="https://github.com/opentofu/opentofu/releases/download/v${VERSION}/tofu_${VERSION}_windows_${ARCH}.zip"
    echo "Downloading OpenTofu for Windows (${ZIP_URL})..."
    curl -fsSL -o .tools/tofu.zip "$ZIP_URL"
    if command -v unzip >/dev/null 2>&1; then
      unzip -o .tools/tofu.zip -d .tools tofu.exe >/dev/null
    elif [ -x "/c/Windows/System32/tar.exe" ]; then
      /c/Windows/System32/tar.exe -xf .tools/tofu.zip -C .tools tofu.exe
    fi
    rm -f .tools/tofu.zip
    export PATH="$(pwd)/.tools:$PATH"
    TOFU_CMD="$(pwd)/.tools/tofu.exe"
  else
    TAR_URL="https://github.com/opentofu/opentofu/releases/download/v${VERSION}/tofu_${VERSION}_linux_${ARCH}.tar.gz"
    echo "Downloading OpenTofu for Linux (${TAR_URL})..."
    curl -fsSL -o .tools/tofu.tar.gz "$TAR_URL"
    tar -xzf .tools/tofu.tar.gz -C .tools tofu
    rm -f .tools/tofu.tar.gz
    chmod +x .tools/tofu
    export PATH="$(pwd)/.tools:$PATH"
    TOFU_CMD="$(pwd)/.tools/tofu"
  fi
  echo "OpenTofu downloaded successfully to .tools/"
elif command -v terraform >/dev/null 2>&1 && ! command -v tofu >/dev/null 2>&1; then
  TOFU_CMD="terraform"
fi

if [ -z "${SUBSCRIPTION_ID}" ]; then
  SUBSCRIPTION_ID=$(az account show --query id -o tsv | tr -d '\r')
fi
echo "Using Subscription ID: ${SUBSCRIPTION_ID}"
az account set --subscription "${SUBSCRIPTION_ID}"

# 2. Register Resource Providers
echo ""
echo "[2/8] Registering Azure Resource Providers..."
for p in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.DocumentDB Microsoft.KeyVault; do
  echo "  Registering $p..."
  az provider register --namespace "$p" --output none
done

# 3. Create OpenTofu State Storage Backend
echo ""
echo "[3/8] Setting up OpenTofu state storage in ${LOCATION}..."
STATE_RG="tofu-state-rg"
STATE_STORAGE="sonarremediationtfstate"
STATE_CONTAINER="tfstate"

if [ "$(az group exists --name "$STATE_RG" | tr -d '\r')" != "true" ]; then
  echo "  Creating resource group $STATE_RG..."
  az group create --name "$STATE_RG" --location "$LOCATION" --output none
fi

if [ "$(az storage account check-name --name "$STATE_STORAGE" --query "nameAvailable" -o tsv | tr -d '\r')" = "true" ]; then
  echo "  Creating storage account $STATE_STORAGE..."
  az storage account create \
    --name "$STATE_STORAGE" \
    --resource-group "$STATE_RG" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --allow-blob-public-access false \
    --output none
fi

echo "  Ensuring blob container '$STATE_CONTAINER' exists..."
az storage container create \
  --name "$STATE_CONTAINER" \
  --account-name "$STATE_STORAGE" \
  --auth-mode login \
  --output none 2>/dev/null || true

# 4. Setup GitHub Deployer OIDC App Registration
echo ""
echo "[4/8] Configuring GitHub Actions OIDC Deployer App..."
APP_NAME="sonar-remediation-github-deployer"
APP_ID=$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv | tr -d '\r')

if [ -z "$APP_ID" ]; then
  echo "  Creating Entra ID Application '$APP_NAME'..."
  APP_ID=$(az ad app create --display-name "$APP_NAME" --query appId -o tsv | tr -d '\r')
fi

SP_ID=$(az ad sp list --display-name "$APP_NAME" --query "[0].id" -o tsv | tr -d '\r')
if [ -z "$SP_ID" ]; then
  echo "  Creating Service Principal for '$APP_NAME'..."
  SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv | tr -d '\r')
fi

CRED_NAME="github-actions-${ENVIRONMENT}"
CRED_EXISTS=$(az ad app federated-credential list --id "$APP_ID" --query "[?name=='$CRED_NAME'].name" -o tsv | tr -d '\r')
if [ -z "$CRED_EXISTS" ]; then
  echo "  Adding federated credential '$CRED_NAME'..."
  FED_PARAMS=$(cat <<EOF
{
  "name": "${CRED_NAME}",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:${GITHUB_REPO}:environment:${ENVIRONMENT}",
  "description": "GitHub Actions OIDC token for ${ENVIRONMENT} environment",
  "audiences": ["api://AzureADTokenExchange"]
}
EOF
)
  TEMP_FILE=$(mktemp)
  echo "$FED_PARAMS" > "$TEMP_FILE"
  az ad app federated-credential create --id "$APP_ID" --parameters "$TEMP_FILE" --output none
  rm -f "$TEMP_FILE"
fi

STATE_SA_ID=$(az storage account show --name "$STATE_STORAGE" --resource-group "$STATE_RG" --query id -o tsv | tr -d '\r')
az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal --role "Storage Blob Data Contributor" --scope "$STATE_SA_ID" --output none 2>/dev/null || true

CURRENT_USER_ID=$(az ad signed-in-user show --query id -o tsv 2>/dev/null | tr -d '\r' || echo "")
if [ -n "$CURRENT_USER_ID" ]; then
  az role assignment create --assignee-object-id "$CURRENT_USER_ID" --assignee-principal-type User --role "Storage Blob Data Contributor" --scope "$STATE_SA_ID" --output none 2>/dev/null || true
fi

# 5. OpenTofu Init & Base Apply
echo ""
echo "[5/8] Running OpenTofu init and base provisioning..."
TOFU_DIR="infra/environments/${ENVIRONMENT}/azure"
cd "$TOFU_DIR"

"$TOFU_CMD" init -input=false \
  -backend-config="resource_group_name=${STATE_RG}" \
  -backend-config="storage_account_name=${STATE_STORAGE}" \
  -backend-config="container_name=${STATE_CONTAINER}" \
  -backend-config="key=${ENVIRONMENT}.tfstate"

echo "  Applying base resources (RG, ACR, Key Vault, Cosmos DB, Identities)..."
"$TOFU_CMD" apply -auto-approve \
  -target="module.azure.azurerm_resource_group.main" \
  -target="module.azure.azurerm_container_registry.acr" \
  -target="module.azure.azurerm_key_vault.main" \
  -target="module.azure.azurerm_cosmosdb_account.main" \
  -target="module.azure.azurerm_user_assigned_identity.agent" \
  -target="module.azure.azurerm_user_assigned_identity.dashboard"

cd - >/dev/null

# 6. Build and Push Initial Docker Images to ACR
echo ""
echo "[6/8] Building and pushing initial container images to ACR..."
USE_PREFIXES=false
if [ "$ENVIRONMENT" = "sb" ] || [[ "$ENVIRONMENT" == sb* ]]; then
  USE_PREFIXES=true
fi

if [ "$USE_PREFIXES" = true ]; then
  APP_RG="rg-${ENVIRONMENT}-sonar-remediation"
  ACR_NAME="acr${ENVIRONMENT}sonarremediation"
  KV_NAME="kv-${ENVIRONMENT}-sonar-remed"
  DASH_ID_NAME="id-${ENVIRONMENT}-sonar-dashboard"
  AGENT_ID_NAME="id-${ENVIRONMENT}-sonar-agent"
else
  APP_RG="sonar-remediation-${ENVIRONMENT}-rg"
  ACR_NAME="sonarremediation${ENVIRONMENT}acr"
  KV_NAME="sonar-${ENVIRONMENT}-kv"
  DASH_ID_NAME="sonar-dashboard-${ENVIRONMENT}-id"
  AGENT_ID_NAME="sonar-agent-${ENVIRONMENT}-id"
fi

echo "  Logging in to $ACR_NAME..."
az acr login --name "$ACR_NAME"

DASH_IMG="${ACR_NAME}.azurecr.io/sonar-dashboard:latest"
AGENT_IMG="${ACR_NAME}.azurecr.io/sonar-remediation-agent:latest"

echo "  Building and pushing Dashboard image ($DASH_IMG)..."
docker build -f dashboard/Dockerfile -t "$DASH_IMG" .
docker push "$DASH_IMG"

echo "  Building and pushing Agent image ($AGENT_IMG)..."
docker build -f Dockerfile -t "$AGENT_IMG" .
docker push "$AGENT_IMG"

# 7. Grant Data-Plane RBAC Roles
echo ""
echo "[7/8] Granting data-plane RBAC roles..."
ACR_ID=$(az acr show --name "$ACR_NAME" --query id -o tsv | tr -d '\r')
DASH_ID=$(az identity show --name "$DASH_ID_NAME" --resource-group "$APP_RG" --query principalId -o tsv | tr -d '\r')
AGENT_ID=$(az identity show --name "$AGENT_ID_NAME" --resource-group "$APP_RG" --query principalId -o tsv | tr -d '\r')
APP_RG_ID=$(az group show --name "$APP_RG" --query id -o tsv | tr -d '\r')
COSMOS_NAME=$(az cosmosdb list --resource-group "$APP_RG" --query "[0].name" -o tsv | tr -d '\r')

# ACR Pull
echo "  Granting AcrPull to managed identities..."
az role assignment create --assignee-object-id "$DASH_ID" --assignee-principal-type ServicePrincipal --role "AcrPull" --scope "$ACR_ID" --output none 2>/dev/null || true
az role assignment create --assignee-object-id "$AGENT_ID" --assignee-principal-type ServicePrincipal --role "AcrPull" --scope "$ACR_ID" --output none 2>/dev/null || true

# Key Vault
echo "  Granting Key Vault roles..."
KV_ID=$(az keyvault show --name "$KV_NAME" --resource-group "$APP_RG" --query id -o tsv | tr -d '\r')
az role assignment create --assignee-object-id "$DASH_ID" --assignee-principal-type ServicePrincipal --role "Key Vault Secrets Officer" --scope "$KV_ID" --output none 2>/dev/null || true
az role assignment create --assignee-object-id "$AGENT_ID" --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User" --scope "$KV_ID" --output none 2>/dev/null || true
az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal --role "Key Vault Administrator" --scope "$KV_ID" --output none 2>/dev/null || true
if [ -n "$CURRENT_USER_ID" ]; then
  az role assignment create --assignee-object-id "$CURRENT_USER_ID" --assignee-principal-type User --role "Key Vault Administrator" --scope "$KV_ID" --output none 2>/dev/null || true
fi

# Container Apps Operator
echo "  Granting Container Apps roles to dashboard identity..."
az role assignment create --assignee-object-id "$DASH_ID" --assignee-principal-type ServicePrincipal --role "Container Apps Contributor" --scope "$APP_RG_ID" --output none 2>/dev/null || true
az role assignment create --assignee-object-id "$DASH_ID" --assignee-principal-type ServicePrincipal --role "Container Apps Jobs Operator" --scope "$APP_RG_ID" --output none 2>/dev/null || true

# Cosmos DB Built-in Data Contributor
echo "  Granting Cosmos DB Built-in Data Contributor..."
ROLE_DEF_ID="00000000-0000-0000-0000-000000000002"
COSMOS_ROLE_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${APP_RG}/providers/Microsoft.DocumentDB/databaseAccounts/${COSMOS_NAME}/sqlRoleDefinitions/${ROLE_DEF_ID}"
az cosmosdb sql role assignment create --account-name "$COSMOS_NAME" --resource-group "$APP_RG" --role-definition-id "$COSMOS_ROLE_SCOPE" --principal-id "$DASH_ID" --scope "/" --output none 2>/dev/null || true
az cosmosdb sql role assignment create --account-name "$COSMOS_NAME" --resource-group "$APP_RG" --role-definition-id "$COSMOS_ROLE_SCOPE" --principal-id "$AGENT_ID" --scope "/" --output none 2>/dev/null || true
if [ -n "$CURRENT_USER_ID" ]; then
  az cosmosdb sql role assignment create --account-name "$COSMOS_NAME" --resource-group "$APP_RG" --role-definition-id "$COSMOS_ROLE_SCOPE" --principal-id "$CURRENT_USER_ID" --scope "/" --output none 2>/dev/null || true
fi

# Deployer permissions on Resource Group and ACR
echo "  Granting Contributor and AcrPush to GitHub deployer SP..."
az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal --role "Contributor" --scope "$APP_RG_ID" --output none 2>/dev/null || true
az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal --role "AcrPush" --scope "$ACR_ID" --output none 2>/dev/null || true

# 8. Full OpenTofu Apply
echo ""
echo "[8/8] Running final OpenTofu apply to provision Container Apps..."
cd "$TOFU_DIR"
"$TOFU_CMD" apply -auto-approve \
  -var="azure_dashboard_image=${DASH_IMG}" \
  -var="azure_agent_image=${AGENT_IMG}"
cd - >/dev/null

TENANT_ID=$(az account show --query tenantId -o tsv | tr -d '\r')

echo ""
echo "========================================================"
echo " Azure Bootstrap Completed Successfully in ${LOCATION}! "
echo "========================================================"
echo ""
echo "Configure the following in GitHub: Settings -> Environments -> ${ENVIRONMENT}"
echo "  AZURE_SUBSCRIPTION_ID : ${SUBSCRIPTION_ID}"
echo "  AZURE_TENANT_ID       : ${TENANT_ID}"
echo "  AZURE_CLIENT_ID       : ${APP_ID}"
echo "  AZURE_RESOURCE_GROUP  : ${APP_RG}"
echo "  AZURE_ACR_NAME        : ${ACR_NAME}"
echo "  AZURE_LOCATION        : ${LOCATION}"
echo ""
echo "Repo-Level Variables:"
echo "  TOFU_STATE_AZURE_RESOURCE_GROUP  : ${STATE_RG}"
echo "  TOFU_STATE_AZURE_STORAGE_ACCOUNT : ${STATE_STORAGE}"
echo "  TOFU_STATE_AZURE_CONTAINER       : ${STATE_CONTAINER}"
echo ""
