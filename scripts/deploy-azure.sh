#!/usr/bin/env bash
# deploy-azure.sh ─ One-shot deploy of ASOE pre-prod to Azure Container Apps.
#
# What it does:
#   1. Verifies az CLI + required providers.
#   2. Creates the resource group if missing.
#   3. Deploys infra/main.bicep (ACR, Postgres, Redis, Container App Env + App).
#   4. Builds the API image with `az acr build` (no local Docker required).
#   5. Updates the Container App revision to the freshly built image tag.
#   6. Reminds the operator to run scripts/set-secrets.sh.
#
# Prerequisites:
#   - Azure CLI 2.55+   (https://aka.ms/InstallAzureCLI)
#   - Logged in:        az login
#   - Selected sub:     az account set --subscription <id>
#
# Usage:
#   PG_ADMIN_PASSWORD='<strong-pw>' ./scripts/deploy-azure.sh
#
# Override defaults via env vars:
#   RG=asoepreprod LOCATION=eastus IMAGE_TAG=v0.3.2 ./scripts/deploy-azure.sh

set -euo pipefail

# ────────────────────────────────────────────────────────── Defaults

: "${SUBSCRIPTION_ID:=f6f24d74-9f1a-4717-94d2-4eef4a617aa0}"
: "${RG:=asoepreprod}"
: "${LOCATION:=eastus}"
: "${NAME_PREFIX:=asoepreprod}"
: "${ACR_NAME:=${NAME_PREFIX}acr}"
: "${APP_NAME:=${NAME_PREFIX}api}"
: "${IMAGE_NAME:=asoe-api}"
: "${IMAGE_TAG:=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
: "${BICEP_FILE:=infra/main.bicep}"
: "${PARAMS_FILE:=infra/parameters.sandbox.json}"

if [[ -z "${PG_ADMIN_PASSWORD:-}" ]]; then
    echo "ERROR: PG_ADMIN_PASSWORD env var is required (set a strong Postgres admin password)." >&2
    echo "       Example: PG_ADMIN_PASSWORD='ReplaceMe!2026' ./scripts/deploy-azure.sh" >&2
    exit 1
fi

echo "── ASOE Azure deploy ────────────────────────────────────────"
echo "Subscription : ${SUBSCRIPTION_ID}"
echo "Resource grp : ${RG}"
echo "Location     : ${LOCATION}"
echo "ACR          : ${ACR_NAME}.azurecr.io"
echo "Container App: ${APP_NAME}"
echo "Image tag    : ${IMAGE_NAME}:${IMAGE_TAG}"
echo "─────────────────────────────────────────────────────────────"

# ────────────────────────────────────── 1. Pre-flight checks

command -v az >/dev/null || { echo "az CLI not found"; exit 1; }
az account show >/dev/null 2>&1 || { echo "Run 'az login' first"; exit 1; }
az account set --subscription "${SUBSCRIPTION_ID}"

# Register the resource providers we touch (idempotent, ~30 s on first run).
for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL \
          Microsoft.Cache Microsoft.OperationalInsights Microsoft.Insights; do
    state=$(az provider show -n "${ns}" --query registrationState -o tsv 2>/dev/null || echo "NotRegistered")
    if [[ "${state}" != "Registered" ]]; then
        echo "Registering provider ${ns} ..."
        az provider register -n "${ns}" --wait
    fi
done

# Ensure containerapp + log-analytics extensions present.
az extension add --name containerapp --upgrade --yes >/dev/null 2>&1 || true
az extension add --name log-analytics --upgrade --yes >/dev/null 2>&1 || true

# ────────────────────────────────────── 2. Resource group

if ! az group show --name "${RG}" >/dev/null 2>&1; then
    echo "Creating resource group ${RG} in ${LOCATION} ..."
    az group create --name "${RG}" --location "${LOCATION}" \
        --tags project=asoe env=sandbox managed=bicep >/dev/null
fi

# ────────────────────────────────────── 3. Bicep deploy

echo "Deploying bicep template (this provisions ACR, Postgres, Redis, Container App; ~10-15 min)..."
az deployment group create \
    --resource-group "${RG}" \
    --name "asoe-deploy-$(date +%Y%m%d%H%M%S)" \
    --template-file "${BICEP_FILE}" \
    --parameters "@${PARAMS_FILE}" \
    --parameters pgAdminPassword="${PG_ADMIN_PASSWORD}" \
    --output table

# ────────────────────────────────────── 4. Build & push image to ACR

echo "Building API image in ACR (uses the cloud builder, no local Docker needed)..."
az acr build \
    --registry "${ACR_NAME}" \
    --image "${IMAGE_NAME}:${IMAGE_TAG}" \
    --image "${IMAGE_NAME}:latest" \
    --file Dockerfile.api \
    .

# ────────────────────────────────────── 5. Update Container App revision

FULL_IMAGE="${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"
echo "Pointing Container App ${APP_NAME} at ${FULL_IMAGE} ..."
az containerapp update \
    --name "${APP_NAME}" \
    --resource-group "${RG}" \
    --image "${FULL_IMAGE}" \
    --output table

FQDN=$(az containerapp show --name "${APP_NAME}" --resource-group "${RG}" \
    --query properties.configuration.ingress.fqdn -o tsv)

echo
echo "── DEPLOY COMPLETE ──────────────────────────────────────────"
echo "API URL      : https://${FQDN}"
echo "Health probe : https://${FQDN}/api/v1/health"
echo
echo "NEXT STEP (required — secrets are empty until you do this):"
echo "  ./scripts/set-secrets.sh"
echo
echo "Then watch logs with:"
echo "  az containerapp logs show -n ${APP_NAME} -g ${RG} --follow"
echo "─────────────────────────────────────────────────────────────"
