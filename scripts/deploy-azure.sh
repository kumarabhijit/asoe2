#!/usr/bin/env bash
# deploy-azure.sh ─ One-shot deploy of ASOE pre-prod to Azure Container Apps.
#
# What it does (three-stage to avoid the placeholder-image probe-failure
# trap and to bake the API FQDN into the UI bundle at build time):
#   1. Verifies az CLI + required providers.
#   2. Creates the resource group if missing.
#   3. Deletes any prior Failed Container App so re-runs start clean.
#   4. STAGE 1 bicep: ACR + Postgres + Redis + Log Analytics + Managed Env
#      + UAMI + AcrPull RBAC (no Container App yet).
#   5. Builds the API image into the now-existing ACR with `az acr build`.
#   6. Builds DATABASE_URL / REDIS_URL from infra outputs (URL-encoded so a
#      password containing '@' or a Redis key with '+' '/' '=' is safe).
#      Reuses any previously-set secrets on re-runs so re-deploys don't
#      wipe them.
#   7. STAGE 2 bicep: API Container App with real image + secrets. Probe
#      and Postgres migration both succeed on first revision. The bicep
#      template uses cae.properties.defaultDomain to compute the future
#      UI FQDN deterministically and includes it in CORS_ALLOWED_ORIGINS,
#      so even before the UI app exists the API is ready to allow it.
#   8. STAGE 3 (only when DEPLOY_UI=1): builds the UI image with the
#      now-resolved API FQDN passed as --build-arg NEXT_PUBLIC_API_URL,
#      then re-runs bicep with deployUiContainerApp=true to provision
#      the UI Container App.
#
# Run from the repo root:
#
#   PG_ADMIN_PASSWORD='<strong-pw>' \
#   ANTHROPIC_API_KEY='sk-ant-...' \           # required on first deploy;
#                                              # preserved on subsequent re-runs
#   ASOE_JWT_SECRET=<hex>|auto \               # optional; preserved on re-runs;
#                                              # auto-generated on first deploy
#   DEPLOY_UI=1 \                              # optional (default 0); when set,
#                                              # also builds & deploys the
#                                              # asoe-ui Container App.
#   ASOE_UI_PATH=../asoe-ui \                  # optional; checkout path of the
#                                              # asoe-ui repo (default
#                                              # ../asoe-ui). If the path is
#                                              # missing the script clones
#                                              # ${ASOE_UI_REPO_URL} (default
#                                              # https://github.com/
#                                              # kumarabhijit/asoe-ui.git) at
#                                              # ${ASOE_UI_BRANCH} (default
#                                              # core_ui_integration) into it.
#   GITHUB_TOKEN=ghp_... \                     # required for the auto-clone
#                                              # if asoe-ui is a private repo.
#                                              # Falls back to GH_TOKEN or
#                                              # GITHUB_CODESPACE_ACCESS. Used
#                                              # only for the clone; not
#                                              # persisted in .git/config.
#   NEXTAUTH_SECRET=<hex>|auto \               # optional; preserved on re-runs;
#                                              # auto-generated on first deploy
#       ./scripts/deploy-azure.sh
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
: "${UI_APP_NAME:=${NAME_PREFIX}ui}"
: "${PG_SERVER:=${NAME_PREFIX}pg}"
: "${PG_DB:=asoe}"
: "${PG_USER:=asoeadmin}"
: "${REDIS_NAME:=${NAME_PREFIX}redis}"
: "${IMAGE_NAME:=asoe-api}"
: "${UI_IMAGE_NAME:=asoe-ui}"
: "${IMAGE_TAG:=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
: "${BICEP_FILE:=infra/main.bicep}"
: "${PARAMS_FILE:=infra/parameters.sandbox.json}"
: "${DEPLOY_UI:=0}"
: "${ASOE_UI_PATH:=../asoe-ui}"

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

# ────────────────────────────────────── 6. Build secrets for Stage 2
#
# DATABASE_URL and REDIS_URL are derived from infra here (not via a
# separate set-secrets.sh step) so re-running this script never wipes
# the live secrets back to placeholders. ANTHROPIC_API_KEY and
# ASOE_JWT_SECRET are preserved across re-runs unless explicitly
# overridden via env var.

# URL-encode so a password containing '@' or a base64 Redis key
# containing '+' '/' '=' is safe to splice into a connection string.
url_encode() {
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

PG_HOST=$(az postgres flexible-server show \
    --name "${PG_SERVER}" --resource-group "${RG}" \
    --query fullyQualifiedDomainName -o tsv)

REDIS_HOST=$(az redisenterprise show \
    --name "${REDIS_NAME}" --resource-group "${RG}" \
    --query hostName -o tsv)

REDIS_KEY=$(az redisenterprise database list-keys \
    --cluster-name "${REDIS_NAME}" --resource-group "${RG}" \
    --query primaryKey -o tsv)

PG_PASS_ENC=$(url_encode "${PG_ADMIN_PASSWORD}")
REDIS_KEY_ENC=$(url_encode "${REDIS_KEY}")

DATABASE_URL="postgresql://${PG_USER}:${PG_PASS_ENC}@${PG_HOST}:5432/${PG_DB}?sslmode=require"
REDIS_URL="rediss://:${REDIS_KEY_ENC}@${REDIS_HOST}:10000"

# Helper: read a current secret value off the existing Container App
# (returns empty string if app or secret doesn't exist).
read_existing_secret() {
    local secret_name="$1"
    az containerapp secret show \
        --resource-group "${RG}" --name "${APP_NAME}" \
        --secret-name "${secret_name}" \
        --query value -o tsv 2>/dev/null || true
}

PLACEHOLDER='placeholder-set-via-set-secrets-sh'

# ANTHROPIC_API_KEY: required on first deploy; preserved on re-runs unless
# the caller passed a new value to rotate it.
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    existing=$(read_existing_secret 'anthropic-api-key')
    if [[ -n "${existing}" && "${existing}" != "${PLACEHOLDER}" ]]; then
        ANTHROPIC_API_KEY="${existing}"
        echo "Preserving existing ANTHROPIC_API_KEY (pass ANTHROPIC_API_KEY=... to rotate)."
    else
        echo "ERROR: ANTHROPIC_API_KEY env var is required on first deploy." >&2
        echo "       Example: ANTHROPIC_API_KEY='sk-ant-...' PG_ADMIN_PASSWORD='...' ./scripts/deploy-azure.sh" >&2
        exit 1
    fi
fi

# ASOE_JWT_SECRET: preserved on re-runs; auto-generated on first deploy
# unless caller passed an explicit value (or 'auto' to force a new one).
if [[ "${ASOE_JWT_SECRET:-}" == "auto" ]]; then
    ASOE_JWT_SECRET=$(openssl rand -hex 64)
    echo "Generated new ASOE_JWT_SECRET ($(echo -n "${ASOE_JWT_SECRET}" | wc -c) chars). Save this if you want to reuse it."
elif [[ -z "${ASOE_JWT_SECRET:-}" ]]; then
    existing=$(read_existing_secret 'asoe-jwt-secret')
    if [[ -n "${existing}" && "${existing}" != "${PLACEHOLDER}" ]]; then
        ASOE_JWT_SECRET="${existing}"
        echo "Preserving existing ASOE_JWT_SECRET (pass ASOE_JWT_SECRET=auto to rotate)."
    else
        ASOE_JWT_SECRET=$(openssl rand -hex 64)
        echo "Generated new ASOE_JWT_SECRET (no existing value to preserve)."
    fi
fi

# ────────────────────────────────────── 7. STAGE 2 bicep: Container App

FULL_IMAGE="${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"
echo "STAGE 2 bicep deploy: Container App with ${FULL_IMAGE} + real secrets (~3-5 min)..."
az deployment group create \
    --resource-group "${RG}" \
    --name "asoe-stage2-$(date +%Y%m%d%H%M%S)" \
    --template-file "${BICEP_FILE}" \
    --parameters "@${PARAMS_FILE}" \
    --parameters pgAdminPassword="${PG_ADMIN_PASSWORD}" \
    --parameters deployContainerApp=true \
    --parameters containerImage="${FULL_IMAGE}" \
    --parameters anthropicApiKey="${ANTHROPIC_API_KEY}" \
    --parameters asoeJwtSecret="${ASOE_JWT_SECRET}" \
    --parameters databaseUrl="${DATABASE_URL}" \
    --parameters redisUrl="${REDIS_URL}" \
    --output table

FQDN=$(az containerapp show --name "${APP_NAME}" --resource-group "${RG}" \
    --query properties.configuration.ingress.fqdn -o tsv)

# ────────────────────────────────────── 8. STAGE 3 (UI) — opt-in
#
# Skipped when DEPLOY_UI=0 (default). When enabled, builds the asoe-ui
# Next.js standalone image into ACR with the API FQDN baked in via
# --build-arg, then re-runs the bicep template with deployUiContainerApp
# =true to provision the Stateless UI Container App. NEXTAUTH_SECRET is
# preserved across re-runs (or auto-generated on first deploy).

UI_FQDN=""
if [[ "${DEPLOY_UI}" == "1" ]]; then
    # Resolve / fetch the asoe-ui checkout. Three modes:
    #   * Path exists with a .git directory   → use as-is.
    #   * Path doesn't exist                   → clone from ASOE_UI_REPO_URL
    #     into ASOE_UI_PATH (defaults to ../asoe-ui sibling). Branch defaults
    #     to ASOE_UI_BRANCH (default core_ui_integration to match this PR;
    #     override to 'main' once merged).
    #   * Path exists but is empty / has no Dockerfile → error (don't risk
    #     overwriting unrelated work).
    : "${ASOE_UI_REPO_URL:=https://github.com/kumarabhijit/asoe-ui.git}"
    : "${ASOE_UI_BRANCH:=core_ui_integration}"

    # PAT support — kumarabhijit/asoe-ui is private. Look for a token in
    # the conventional env vars and splice it into the clone URL using
    # GitHub's `x-access-token:<PAT>` form. After the clone we rewrite
    # the remote URL back to the plain form so the PAT does not get
    # persisted in `.git/config`.
    GH_PAT="${GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_CODESPACE_ACCESS:-}}}"

    if [[ ! -d "${ASOE_UI_PATH}/.git" ]]; then
        if [[ -e "${ASOE_UI_PATH}" ]] && [[ -n "$(ls -A "${ASOE_UI_PATH}" 2>/dev/null)" ]]; then
            echo "ERROR: ASOE_UI_PATH '${ASOE_UI_PATH}' exists but is not a git checkout." >&2
            echo "       Move it aside or pick a different ASOE_UI_PATH." >&2
            exit 1
        fi

        if [[ -n "${GH_PAT}" ]]; then
            # Splice the PAT into the URL only for the duration of the clone.
            authed_url="${ASOE_UI_REPO_URL/https:\/\//https://x-access-token:${GH_PAT}@}"
            echo "Cloning asoe-ui (${ASOE_UI_BRANCH}) into ${ASOE_UI_PATH} (using PAT) ..."
            git clone --branch "${ASOE_UI_BRANCH}" --depth 1 \
                "${authed_url}" "${ASOE_UI_PATH}"
            # Scrub the PAT from the remote URL so it doesn't sit in
            # .git/config on disk.
            git -C "${ASOE_UI_PATH}" remote set-url origin "${ASOE_UI_REPO_URL}"
        else
            echo "Cloning asoe-ui (${ASOE_UI_BRANCH}) into ${ASOE_UI_PATH} ..."
            if ! git clone --branch "${ASOE_UI_BRANCH}" --depth 1 \
                "${ASOE_UI_REPO_URL}" "${ASOE_UI_PATH}" 2>/dev/null; then
                echo "ERROR: clone failed. asoe-ui is private — set a PAT in one of:" >&2
                echo "       GITHUB_TOKEN | GH_TOKEN | GITHUB_CODESPACE_ACCESS" >&2
                echo "       Example:" >&2
                echo "         GITHUB_TOKEN='ghp_...' DEPLOY_UI=1 PG_ADMIN_PASSWORD='...' ./scripts/deploy-azure.sh" >&2
                echo "       Or pre-clone manually:" >&2
                echo "         git clone https://<your-pat>@github.com/kumarabhijit/asoe-ui.git ${ASOE_UI_PATH}" >&2
                exit 1
            fi
        fi
    fi

    if [[ ! -f "${ASOE_UI_PATH}/Dockerfile" ]]; then
        echo "ERROR: ${ASOE_UI_PATH}/Dockerfile not found. Make sure the asoe-ui core_ui_integration branch is checked out (the Dockerfile only exists on that branch yet)." >&2
        exit 1
    fi

    # NEXTAUTH_SECRET preservation (mirrors the ANTHROPIC_API_KEY pattern).
    NEXTAUTH_PLACEHOLDER='placeholder-set-by-deploy-script'
    read_existing_ui_secret() {
        local secret_name="$1"
        az containerapp secret show \
            --resource-group "${RG}" --name "${UI_APP_NAME}" \
            --secret-name "${secret_name}" \
            --query value -o tsv 2>/dev/null || true
    }
    if [[ "${NEXTAUTH_SECRET:-}" == "auto" ]]; then
        NEXTAUTH_SECRET=$(openssl rand -hex 64)
        echo "Generated new NEXTAUTH_SECRET ($(echo -n "${NEXTAUTH_SECRET}" | wc -c) chars)."
    elif [[ -z "${NEXTAUTH_SECRET:-}" ]]; then
        existing=$(read_existing_ui_secret 'nextauth-secret')
        if [[ -n "${existing}" && "${existing}" != "${NEXTAUTH_PLACEHOLDER}" ]]; then
            NEXTAUTH_SECRET="${existing}"
            echo "Preserving existing NEXTAUTH_SECRET (pass NEXTAUTH_SECRET=auto to rotate)."
        else
            NEXTAUTH_SECRET=$(openssl rand -hex 64)
            echo "Generated new NEXTAUTH_SECRET (no existing value to preserve)."
        fi
    fi

    echo "Building UI image in ACR with NEXT_PUBLIC_API_URL=https://${FQDN} ..."
    az acr build \
        --registry "${ACR_NAME}" \
        --image "${UI_IMAGE_NAME}:${IMAGE_TAG}" \
        --image "${UI_IMAGE_NAME}:latest" \
        --file Dockerfile \
        --build-arg "NEXT_PUBLIC_API_URL=https://${FQDN}" \
        --build-arg "NEXT_PUBLIC_USE_REAL_API=1" \
        "${ASOE_UI_PATH}"

    UI_FULL_IMAGE="${ACR_NAME}.azurecr.io/${UI_IMAGE_NAME}:${IMAGE_TAG}"
    echo "STAGE 3 bicep deploy: UI Container App with ${UI_FULL_IMAGE} (~3 min)..."
    az deployment group create \
        --resource-group "${RG}" \
        --name "asoe-stage3-$(date +%Y%m%d%H%M%S)" \
        --template-file "${BICEP_FILE}" \
        --parameters "@${PARAMS_FILE}" \
        --parameters pgAdminPassword="${PG_ADMIN_PASSWORD}" \
        --parameters deployContainerApp=true \
        --parameters deployUiContainerApp=true \
        --parameters containerImage="${FULL_IMAGE}" \
        --parameters containerImageUi="${UI_FULL_IMAGE}" \
        --parameters anthropicApiKey="${ANTHROPIC_API_KEY}" \
        --parameters asoeJwtSecret="${ASOE_JWT_SECRET}" \
        --parameters databaseUrl="${DATABASE_URL}" \
        --parameters redisUrl="${REDIS_URL}" \
        --parameters nextAuthSecret="${NEXTAUTH_SECRET}" \
        --output table

    UI_FQDN=$(az containerapp show --name "${UI_APP_NAME}" --resource-group "${RG}" \
        --query properties.configuration.ingress.fqdn -o tsv)
fi

echo
echo "── DEPLOY COMPLETE ──────────────────────────────────────────"
echo "API URL      : https://${FQDN}"
echo "Health probe : https://${FQDN}/api/v1/health"
if [[ -n "${UI_FQDN}" ]]; then
    echo "UI URL       : https://${UI_FQDN}"
    echo "UI sign-in   : https://${UI_FQDN}/login"
fi
echo
echo "Verify API:   curl -fsS --max-time 30 https://${FQDN}/api/v1/health | jq ."
echo "Tail API:     az containerapp logs show -n ${APP_NAME} -g ${RG} --follow"
if [[ -n "${UI_FQDN}" ]]; then
    echo "Tail UI:      az containerapp logs show -n ${UI_APP_NAME} -g ${RG} --follow"
fi
echo
echo "Rotate the Anthropic key without redeploying infra:"
echo "  ANTHROPIC_API_KEY='sk-ant-NEW' ./scripts/set-secrets.sh"
if [[ "${DEPLOY_UI}" != "1" ]]; then
    echo
    echo "To deploy the UI Container App alongside the API, re-run with:"
    echo "  DEPLOY_UI=1 ASOE_UI_PATH=../asoe-ui ./scripts/deploy-azure.sh"
fi
echo "─────────────────────────────────────────────────────────────"
