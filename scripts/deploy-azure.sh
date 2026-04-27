#!/usr/bin/env bash
# deploy-azure.sh ─ One-shot deploy of ASOE pre-prod to Azure Container Apps.
#
# What it does (two-stage to avoid the placeholder-image probe-failure trap):
#   1. Verifies az CLI + required providers.
#   2. Creates the resource group if missing.
#   3. Deletes any prior Failed Container App so re-runs start clean.
#   4. STAGE 1 bicep: ACR + Postgres + Redis + Log Analytics + Managed Env
#      (no Container App yet — deployContainerApp=false).
#   5. Builds the API image into the now-existing ACR with `az acr build`.
#   6. STAGE 2 bicep: same template, deployContainerApp=true,
#      containerImage=<the-just-built-image>. The Container App is created
#      with the real image, so the /api/v1/health probe passes on the
#      first revision and the deploy doesn't time out.
#   7. Reminds the operator to run scripts/set-secrets.sh.
#
# Run from the repo root:
#   PG_ADMIN_PASSWORD='<strong-pw>' ./scripts/deploy-azure.sh
#
# Prerequisites:
#   - Azure CLI 2.55+   (https://aka.ms/InstallAzureCLI)
#   - Logged in:        az login
#   - Selected sub:     az account set --subscription <id>
#
# Override defaults via env vars:
#   RG=asoepreprod LOCATION=centralus IMAGE_TAG=v0.3.2 ./scripts/deploy-azure.sh

set -euo pipefail

# ────────────────────────────────────────────────────────── Defaults

: "${SUBSCRIPTION_ID:=f6f24d74-9f1a-4717-94d2-4eef4a617aa0}"
: "${RG:=asoepreprod}"
: "${LOCATION:=centralus}"
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
          Microsoft.Cache Microsoft.OperationalInsights Microsoft.Insights \
          Microsoft.ManagedIdentity; do
    state=$(az provider show -n "${ns}" --query registrationState -o tsv 2>/dev/null || echo "NotRegistered")
    if [[ "${state}" != "Registered" ]]; then
        echo "Registering provider ${ns} ..."
        az provider register -n "${ns}" --wait
    fi
done

# Ensure containerapp + log-analytics + redisenterprise extensions present.
# (redisenterprise is needed by scripts/set-secrets.sh after this script runs.)
az extension add --name containerapp --upgrade --yes >/dev/null 2>&1 || true
az extension add --name log-analytics --upgrade --yes >/dev/null 2>&1 || true
az extension add --name redisenterprise --upgrade --yes >/dev/null 2>&1 || true

# ────────────────────────────────────── 2. Resource group

if ! az group show --name "${RG}" >/dev/null 2>&1; then
    echo "Creating resource group ${RG} in ${LOCATION} ..."
    az group create --name "${RG}" --location "${LOCATION}" \
        --tags project=asoe env=sandbox managed=bicep >/dev/null
fi

# ────────────────────────────────────── 3. Clean up prior failed app
#
# A previous run may have left the Container App in 'Failed' provisioning
# state (e.g. probe timeout against the placeholder image). Stage 2 below
# can succeed against an existing healthy app, but cannot rescue one whose
# initial provision failed — Azure refuses to overwrite. Delete it here.

if az containerapp show -n "${APP_NAME}" -g "${RG}" >/dev/null 2>&1; then
    state=$(az containerapp show -n "${APP_NAME}" -g "${RG}" \
        --query properties.provisioningState -o tsv 2>/dev/null || echo "Unknown")
    if [[ "${state}" == "Failed" ]]; then
        echo "Removing previously-failed Container App ${APP_NAME} (state=${state}) ..."
        az containerapp delete -n "${APP_NAME}" -g "${RG}" --yes --no-wait || true
        # Wait for the delete to actually take effect (~30 s typical).
        until ! az containerapp show -n "${APP_NAME}" -g "${RG}" >/dev/null 2>&1; do
            sleep 5
        done
    fi
fi

# ────────────────────────────────────── 4. STAGE 1 bicep: shared infra

echo "STAGE 1 bicep deploy: ACR + Postgres + Redis + Log Analytics + Managed Env (~10 min)..."
az deployment group create \
    --resource-group "${RG}" \
    --name "asoe-stage1-$(date +%Y%m%d%H%M%S)" \
    --template-file "${BICEP_FILE}" \
    --parameters "@${PARAMS_FILE}" \
    --parameters pgAdminPassword="${PG_ADMIN_PASSWORD}" \
    --parameters deployContainerApp=false \
    --output table

# ────────────────────────────────────── 5. Build & push image to ACR

echo "Building API image in ACR (cloud builder, no local Docker needed)..."
az acr build \
    --registry "${ACR_NAME}" \
    --image "${IMAGE_NAME}:${IMAGE_TAG}" \
    --image "${IMAGE_NAME}:latest" \
    --file Dockerfile.api \
    .

# ────────────────────────────────────── 6. STAGE 2 bicep: Container App

FULL_IMAGE="${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"
echo "STAGE 2 bicep deploy: Container App with ${FULL_IMAGE} (~3-5 min)..."
az deployment group create \
    --resource-group "${RG}" \
    --name "asoe-stage2-$(date +%Y%m%d%H%M%S)" \
    --template-file "${BICEP_FILE}" \
    --parameters "@${PARAMS_FILE}" \
    --parameters pgAdminPassword="${PG_ADMIN_PASSWORD}" \
    --parameters deployContainerApp=true \
    --parameters containerImage="${FULL_IMAGE}" \
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
