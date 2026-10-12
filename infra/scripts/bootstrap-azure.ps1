<#
.SYNOPSIS
    Bootstraps the Azure infrastructure for the Sonar Remediation Agent from scratch.

.DESCRIPTION
    Provisions all required Azure resources in the specified region (default: centralindia):
    1. Registers required Azure resource providers.
    2. Creates state storage (tofu-state-rg / sonarremediationtfstate).
    3. Configures Entra ID GitHub OIDC deployer app and federated credential.
    4. Runs OpenTofu init and targeted base apply (RG, ACR, Key Vault, Cosmos DB, Identities).
    5. Builds and pushes initial Docker images to ACR.
    6. Configures data-plane and management RBAC roles.
    7. Runs full OpenTofu apply to spin up Container Apps environment, dashboard, and agent jobs.
    8. Prints GitHub Actions environment variables for CI/CD.

.PARAMETER Environment
    Target environment: 'sandbox' (default) or 'prod'.

.PARAMETER Location
    Azure region: 'centralindia' (default).

.PARAMETER SubscriptionId
    Azure Subscription ID. Defaults to current active Azure CLI subscription.

.PARAMETER GitHubRepo
    GitHub repository identifier for OIDC sub claim.
    Defaults to 'thararaoganji@176417932/remediation-agent@1322380487'.
#>

[CmdletBinding()]
param(
    [ValidateSet("sandbox", "prod", "sb")]
    [string]$Environment = "sb",

    [string]$Location = "centralindia",

    [string]$SubscriptionId = "",

    [string]$GitHubRepo = "thararaoganji@176417932/remediation-agent@1322380487"
)

$ErrorActionPreference = "Stop"

Write-Host "=== Sonar Remediation Agent: Azure Bootstrap ===" -ForegroundColor Cyan
Write-Host "Environment: $Environment"
Write-Host "Location:    $Location"

# 1. Validate Prerequisites
Write-Host "`n[1/8] Checking prerequisites..." -ForegroundColor Yellow
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI ('az') is not installed or not on PATH."
}
$tofuCmd = "tofu"
if (-not (Get-Command tofu -ErrorAction SilentlyContinue)) {
    if (Get-Command terraform -ErrorAction SilentlyContinue) {
        $tofuCmd = "terraform"
    } else {
        Write-Host "  OpenTofu not found on PATH. Automatically downloading OpenTofu standalone binary..." -ForegroundColor Yellow
        $toolsDir = Join-Path (Get-Location) ".tools"
        if (-not (Test-Path $toolsDir)) { New-Item -ItemType Directory -Path $toolsDir | Out-Null }
        
        $version = "1.9.0"
        $zipPath = Join-Path $toolsDir "tofu.zip"
        $tofuExe = Join-Path $toolsDir "tofu.exe"
        
        if (-not (Test-Path $tofuExe)) {
            $url = "https://github.com/opentofu/opentofu/releases/download/v$version/tofu_${version}_windows_amd64.zip"
            Write-Host "  Downloading from $url..."
            Invoke-WebRequest -Uri $url -OutFile $zipPath
            Expand-Archive -Path $zipPath -DestinationPath $toolsDir -Force
            Remove-Item $zipPath -Force
        }
        $env:PATH = "$toolsDir;" + $env:PATH
        $tofuCmd = $tofuExe
        Write-Host "  OpenTofu ready at $tofuExe" -ForegroundColor Green
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed or not on PATH."
}

# Resolve Subscription
if ([string]::IsNullOrWhiteSpace($SubscriptionId)) {
    $SubscriptionId = (az account show --query id -o tsv).Trim()
}
Write-Host "Using Subscription ID: $SubscriptionId"
az account set --subscription $SubscriptionId

# 2. Register Resource Providers
Write-Host "`n[2/8] Registering Azure Resource Providers..." -ForegroundColor Yellow
$providers = @(
    "Microsoft.App",
    "Microsoft.OperationalInsights",
    "Microsoft.ContainerRegistry",
    "Microsoft.DocumentDB",
    "Microsoft.KeyVault"
)
foreach ($p in $providers) {
    Write-Host "  Registering $p..."
    az provider register --namespace $p --output none
}

# 3. Create OpenTofu State Storage Backend
Write-Host "`n[3/8] Setting up OpenTofu state storage in $Location..." -ForegroundColor Yellow
$stateRg = "tofu-state-rg"
$stateStorage = "sonarremediationtfstate"
$stateContainer = "tfstate"

$rgExists = (az group exists --name $stateRg).Trim()
if ($rgExists -ne "true") {
    Write-Host "  Creating resource group $stateRg..."
    az group create --name $stateRg --location $Location --output none
}

$saExists = (az storage account check-name --name $stateStorage --query "nameAvailable" -o tsv).Trim()
if ($saExists -eq "true") {
    Write-Host "  Creating storage account $stateStorage..."
    az storage account create `
        --name $stateStorage `
        --resource-group $stateRg `
        --location $Location `
        --sku Standard_LRS `
        --kind StorageV2 `
        --allow-blob-public-access false `
        --output none
}

Write-Host "  Ensuring blob container '$stateContainer' exists..."
az storage container create `
    --name $stateContainer `
    --account-name $stateStorage `
    --auth-mode login `
    --output none 2>$null

# 4. Setup GitHub Deployer OIDC App Registration
Write-Host "`n[4/8] Configuring GitHub Actions OIDC Deployer App..." -ForegroundColor Yellow
$appName = "sonar-remediation-github-deployer"
$appId = (az ad app list --display-name $appName --query "[0].appId" -o tsv)

if ([string]::IsNullOrWhiteSpace($appId)) {
    Write-Host "  Creating Entra ID Application '$appName'..."
    $appId = (az ad app create --display-name $appName --query appId -o tsv).Trim()
}

$spId = (az ad sp list --display-name $appName --query "[0].id" -o tsv)
if ([string]::IsNullOrWhiteSpace($spId)) {
    Write-Host "  Creating Service Principal for '$appName'..."
    $spId = (az ad sp create --id $appId --query id -o tsv).Trim()
}

# Add Federated Credential for GitHub Actions
$credName = "github-actions-$Environment"
$credExists = az ad app federated-credential list --id $appId --query "[?name=='$credName'].name" -o tsv
if ([string]::IsNullOrWhiteSpace($credExists)) {
    Write-Host "  Adding federated credential '$credName'..."
    $fedParams = @{
        name = $credName
        issuer = "https://token.actions.githubusercontent.com"
        subject = "repo:$GitHubRepo:environment:$Environment"
        description = "GitHub Actions OIDC token for $Environment environment"
        audiences = @("api://AzureADTokenExchange")
    } | ConvertTo-Json -Compress

    $tempFile = [System.IO.Path]::GetTempFileName()
    Set-Content -Path $tempFile -Value $fedParams
    az ad app federated-credential create --id $appId --parameters $tempFile --output none
    Remove-Item $tempFile -Force
}

# Grant Deployer SP and Current User Storage Blob Data Contributor on State Account
$stateSaId = (az storage account show --name $stateStorage --resource-group $stateRg --query id -o tsv).Trim()
az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal --role "Storage Blob Data Contributor" --scope $stateSaId --output none 2>$null

$currentUserId = (az ad signed-in-user show --query id -o tsv 2>$null)
if (-not [string]::IsNullOrWhiteSpace($currentUserId)) {
    az role assignment create --assignee-object-id $currentUserId --assignee-principal-type User --role "Storage Blob Data Contributor" --scope $stateSaId --output none 2>$null
}

# 5. OpenTofu Init & Base Apply
Write-Host "`n[5/8] Running OpenTofu init and base provisioning..." -ForegroundColor Yellow
$tofuDir = "infra/environments/$Environment/azure"
Push-Location $tofuDir

try {
    & $tofuCmd init -input=false `
        -backend-config="resource_group_name=$stateRg" `
        -backend-config="storage_account_name=$stateStorage" `
        -backend-config="container_name=$stateContainer" `
        -backend-config="key=$Environment.tfstate"

    Write-Host "  Applying base resources (RG, ACR, Key Vault, Cosmos DB, Identities)..."
    & $tofuCmd apply -auto-approve `
        -target="module.azure.azurerm_resource_group.main" `
        -target="module.azure.azurerm_container_registry.acr" `
        -target="module.azure.azurerm_key_vault.main" `
        -target="module.azure.azurerm_cosmosdb_account.main" `
        -target="module.azure.azurerm_user_assigned_identity.agent" `
        -target="module.azure.azurerm_user_assigned_identity.dashboard"
} finally {
    Pop-Location
}

# 6. Build and Push Initial Docker Images to ACR
Write-Host "`n[6/8] Building and pushing initial container images to ACR..." -ForegroundColor Yellow
$usePrefixes = ($Environment -eq "sb" -or $Environment.StartsWith("sb"))
$appRg = if ($usePrefixes) { "rg-$Environment-sonar-remediation" } else { "sonar-remediation-$Environment-rg" }
$acrName = if ($usePrefixes) { "acr${Environment}sonarremediation" } else { "sonarremediation${Environment}acr" }
$kvName = if ($usePrefixes) { "kv-$Environment-sonar-remed" } else { "sonar-$Environment-kv" }
$dashIdName = if ($usePrefixes) { "id-$Environment-sonar-dashboard" } else { "sonar-dashboard-$Environment-id" }
$agentIdName = if ($usePrefixes) { "id-$Environment-sonar-agent" } else { "sonar-agent-$Environment-id" }

Write-Host "  Logging in to $acrName..."
az acr login --name $acrName

$dashImg = "${acrName}.azurecr.io/sonar-dashboard:latest"
$agentImg = "${acrName}.azurecr.io/sonar-remediation-agent:latest"

Write-Host "  Building and pushing Dashboard image ($dashImg)..."
docker build -f dashboard/Dockerfile -t $dashImg .
docker push $dashImg

Write-Host "  Building and pushing Agent image ($agentImg)..."
docker build -f Dockerfile -t $agentImg .
docker push $agentImg

# 7. Grant Data-Plane RBAC Roles
Write-Host "`n[7/8] Granting data-plane RBAC roles..." -ForegroundColor Yellow
$acrId = (az acr show --name $acrName --query id -o tsv).Trim()
$dashId = (az identity show --name $dashIdName --resource-group $appRg --query principalId -o tsv).Trim()
$agentId = (az identity show --name $agentIdName --resource-group $appRg --query principalId -o tsv).Trim()
$appRgId = (az group show --name $appRg --query id -o tsv).Trim()
$cosmosName = (az cosmosdb list --resource-group $appRg --query "[0].name" -o tsv).Trim()

# ACR Pull
Write-Host "  Granting AcrPull to managed identities..."
az role assignment create --assignee-object-id $dashId --assignee-principal-type ServicePrincipal --role "AcrPull" --scope $acrId --output none 2>$null
az role assignment create --assignee-object-id $agentId --assignee-principal-type ServicePrincipal --role "AcrPull" --scope $acrId --output none 2>$null

# Key Vault Secrets
Write-Host "  Granting Key Vault roles..."
$kvId = (az keyvault show --name $kvName --resource-group $appRg --query id -o tsv).Trim()
az role assignment create --assignee-object-id $dashId --assignee-principal-type ServicePrincipal --role "Key Vault Secrets Officer" --scope $kvId --output none 2>$null
az role assignment create --assignee-object-id $agentId --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User" --scope $kvId --output none 2>$null
az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal --role "Key Vault Administrator" --scope $kvId --output none 2>$null

# Container Apps Operator
Write-Host "  Granting Container Apps roles to dashboard identity..."
az role assignment create --assignee-object-id $dashId --assignee-principal-type ServicePrincipal --role "Container Apps Contributor" --scope $appRgId --output none 2>$null
az role assignment create --assignee-object-id $dashId --assignee-principal-type ServicePrincipal --role "Container Apps Jobs Operator" --scope $appRgId --output none 2>$null

# Cosmos DB Built-in Data Contributor
Write-Host "  Granting Cosmos DB Built-in Data Contributor..."
$roleDefId = "00000000-0000-0000-0000-000000000002"
$cosmosRoleScope = "/subscriptions/$SubscriptionId/resourceGroups/$appRg/providers/Microsoft.DocumentDB/databaseAccounts/$cosmosName/sqlRoleDefinitions/$roleDefId"
az cosmosdb sql role assignment create --account-name $cosmosName --resource-group $appRg --role-definition-id $cosmosRoleScope --principal-id $dashId --scope "/" --output none 2>$null
az cosmosdb sql role assignment create --account-name $cosmosName --resource-group $appRg --role-definition-id $cosmosRoleScope --principal-id $agentId --scope "/" --output none 2>$null
if (-not [string]::IsNullOrWhiteSpace($currentUserId)) {
    az cosmosdb sql role assignment create --account-name $cosmosName --resource-group $appRg --role-definition-id $cosmosRoleScope --principal-id $currentUserId --scope "/" --output none 2>$null
}

# Deployer permissions on Resource Group and ACR
Write-Host "  Granting Contributor and AcrPush to GitHub deployer SP..."
az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal --role "Contributor" --scope $appRgId --output none 2>$null
az role assignment create --assignee-object-id $spId --assignee-principal-type ServicePrincipal --role "AcrPush" --scope $acrId --output none 2>$null

# 8. Full OpenTofu Apply
Write-Host "`n[8/8] Running final OpenTofu apply to provision Container Apps..." -ForegroundColor Yellow
Push-Location $tofuDir
try {
    & $tofuCmd apply -auto-approve `
        -var="azure_dashboard_image=$dashImg" `
        -var="azure_agent_image=$agentImg"
} finally {
    Pop-Location
}

# Output Summary
$tenantId = (az account show --query tenantId -o tsv).Trim()
Write-Host "`n========================================================" -ForegroundColor Green
Write-Host " Azure Bootstrap Completed Successfully in $Location! " -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host "`nConfigure the following in GitHub: Settings -> Environments -> $Environment" -ForegroundColor Cyan
Write-Host "  AZURE_SUBSCRIPTION_ID : $SubscriptionId"
Write-Host "  AZURE_TENANT_ID       : $tenantId"
Write-Host "  AZURE_CLIENT_ID       : $appId"
Write-Host "  AZURE_RESOURCE_GROUP  : $appRg"
Write-Host "  AZURE_ACR_NAME        : $acrName"
Write-Host "  AZURE_LOCATION        : $Location"
Write-Host "`nRepo-Level Variables:" -ForegroundColor Cyan
Write-Host "  TOFU_STATE_AZURE_RESOURCE_GROUP  : $stateRg"
Write-Host "  TOFU_STATE_AZURE_STORAGE_ACCOUNT : $stateStorage"
Write-Host "  TOFU_STATE_AZURE_CONTAINER       : $stateContainer"
Write-Host ""
