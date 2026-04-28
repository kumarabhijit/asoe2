#!/usr/bin/env bash
# redeploy-ui.sh ─ Rebuild and redeploy ONLY the asoe-ui Container App.
#
# Use this for fast iterations on the UI image (e.g. after merging a UI
# branch) without touching the API, Postgres, Redis, or any infra.
#
# Prerequisites:
#   * The full deploy has been run at least once with DEPLOY_UI=1, so
#     the UI Container App and ACR exist.
#   * The asoe-ui repo is checked out at $ASOE_UI_PATH (default: ../asoe-ui).
#   * NEXTAUTH_SECRET on the running app is preserved by the bicep
#     template default; pass NEXTAUTH_SECRET=auto to rotate it.
#
# Run from the repo root:
#
#   ./scripts/redeploy-ui.sh
#   ASOE_UI_PATH=/path/to/asoe-ui ./scripts/redeploy-ui.sh
#   IMAGE_TAG=ui-fix-1 ./scripts/redeploy-ui.sh
#   NEXTAUTH_SECRET=auto ./scripts/redeploy-ui.sh           # rotate cookie key

set -euo pipefail

: "${SUBSCRIPTION_ID:=f6f24d74-9f1a-4717-94d2-4eef4a617aa0}"
: "${RG:=asoepreprod}"
: "${NAME_PREFIX:=asoepreprod}"
: "${ACR_NAME:=${NAME_PREFIX}acr}"
: "${APP_NAME:=${NAME_PREFIX}api}"
: "${UI_APP_NAME:=${NAME_PREFIX}ui}"
: "${UI_IMAGE_NAME:=asoe-ui}"
: "${IMAGE_TAG:=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
: "${ASOE_UI_PATH:=../asoe-ui}"

command -v az >/dev/null || { echo "az CLI not found"; exit 1; }
az account show >/dev/null 2>&1 || { echo "Run 'az login' first"; exit 1; }
az account set --subscription "${SUBSCRIPTION_ID}"

: "${ASOE_UI_REPO_URL:=https://github.com/kumarabhijit/asoe-ui.git}"
: "${ASOE_UI_BRANCH:=core_ui_integration}"

# PAT support — same as deploy-azure.sh. Token spliced into the clone
# URL only for the duration of the clone / fetch, then scrubbed from
# the remote so it doesn't persist in .git/config.
GH_PAT="${GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_CODESPACE_ACCESS:-}}}"

if [[ ! -d "${ASOE_UI_PATH}/.git" ]]; then
    if [[ -e "${ASOE_UI_PATH}" ]] && [[ -n "$(ls -A "${ASOE_UI_PATH}" 2>/dev/null)" ]]; then
        echo "ERROR: ASOE_UI_PATH '${ASOE_UI_PATH}' exists but is not a git checkout." >&2
        echo "       Move it aside or pick a different ASOE_UI_PATH." >&2
        exit 1
    fi

    if [[ -n "${GH_PAT}" ]]; then
        authed_url="${ASOE_UI_REPO_URL/https:\/\//https://x-access-token:${GH_PAT}@}"
        echo "Cloning asoe-ui (${ASOE_UI_BRANCH}) into ${ASOE_UI_PATH} (using PAT) ..."
        git clone --branch "${ASOE_UI_BRANCH}" --depth 1 \
            "${authed_url}" "${ASOE_UI_PATH}"
        git -C "${ASOE_UI_PATH}" remote set-url origin "${ASOE_UI_REPO_URL}"
    else
        echo "Cloning asoe-ui (${ASOE_UI_BRANCH}) into ${ASOE_UI_PATH} ..."
        if ! git clone --branch "${ASOE_UI_BRANCH}" --depth 1 \
            "${ASOE_UI_REPO_URL}" "${ASOE_UI_PATH}" 2>/dev/null; then
            echo "ERROR: clone failed. asoe-ui is private — set a PAT in one of:" >&2
            echo "       GITHUB_TOKEN | GH_TOKEN | GITHUB_CODESPACE_ACCESS" >&2
            exit 1
        fi
    fi
else
    # Existing checkout — make sure it's on ${ASOE_UI_BRANCH} and current
    # with origin. Same belt-and-braces logic as deploy-azure.sh:
    #   (a) wrong branch  → fetch + checkout
    #   (b) right branch  → fetch + reset --hard so the deploy always
    #       picks up the latest origin commits without requiring the
    #       user to `git pull` manually (which fails 403 against the
    #       private repo when the PAT was scrubbed from .git/config).
    pat_args=()
    if [[ -n "${GH_PAT}" ]]; then
        pat_args=(-c "http.extraHeader=AUTHORIZATION: bearer ${GH_PAT}")
    fi

    current_branch=$(git -C "${ASOE_UI_PATH}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [[ "${current_branch}" != "${ASOE_UI_BRANCH}" ]]; then
        echo "asoe-ui is on '${current_branch}'; switching to '${ASOE_UI_BRANCH}' ..."
        git -C "${ASOE_UI_PATH}" "${pat_args[@]}" fetch origin "${ASOE_UI_BRANCH}"
        git -C "${ASOE_UI_PATH}" checkout -B "${ASOE_UI_BRANCH}" "origin/${ASOE_UI_BRANCH}"
    else
        echo "asoe-ui is on '${ASOE_UI_BRANCH}'; fetching latest from origin ..."
        git -C "${ASOE_UI_PATH}" "${pat_args[@]}" fetch origin "${ASOE_UI_BRANCH}"
        git -C "${ASOE_UI_PATH}" reset --hard "origin/${ASOE_UI_BRANCH}"
    fi
fi

actual_branch=$(git -C "${ASOE_UI_PATH}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "<unknown>")
actual_sha=$(git -C "${ASOE_UI_PATH}" rev-parse --short HEAD 2>/dev/null || echo "<unknown>")
echo "asoe-ui checkout: branch=${actual_branch} sha=${actual_sha}"

if [[ ! -f "${ASOE_UI_PATH}/Dockerfile" ]]; then
    echo "ERROR: ${ASOE_UI_PATH}/Dockerfile not found." >&2
    echo "       Branch in the checkout: ${actual_branch}" >&2
    echo "       HEAD sha:               ${actual_sha}" >&2
    echo "       Top-level entries:" >&2
    ls -1 "${ASOE_UI_PATH}" | sed 's/^/         /' >&2
    echo "       Override the branch with ASOE_UI_BRANCH=<name> if Dockerfile lives elsewhere." >&2
    exit 1
fi

# Resolve the API FQDN from the existing API Container App so the UI
# bundle is built against the same backend it targets today.
API_FQDN=$(az containerapp show --name "${APP_NAME}" --resource-group "${RG}" \
    --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)
if [[ -z "${API_FQDN}" ]]; then
    echo "ERROR: ${APP_NAME} not found. Run ./scripts/deploy-azure.sh first." >&2
    exit 1
fi

UI_FULL_IMAGE="${ACR_NAME}.azurecr.io/${UI_IMAGE_NAME}:${IMAGE_TAG}"

echo "Building UI image (NEXT_PUBLIC_API_URL=https://${API_FQDN}) ..."
# Build from inside the checkout so `--file Dockerfile .` is unambiguous.
(
    cd "${ASOE_UI_PATH}"
    az acr build \
        --registry "${ACR_NAME}" \
        --image "${UI_IMAGE_NAME}:${IMAGE_TAG}" \
        --image "${UI_IMAGE_NAME}:latest" \
        --file Dockerfile \
        --build-arg "NEXT_PUBLIC_API_URL=https://${API_FQDN}" \
        --build-arg "NEXT_PUBLIC_USE_REAL_API=1" \
        .
)

# Optionally rotate NEXTAUTH_SECRET. Container App secret update doesn't
# require a fresh revision, but the runtime won't pick up the new value
# until the revision is restarted (handled below).
if [[ "${NEXTAUTH_SECRET:-}" == "auto" ]]; then
    NEW_SECRET=$(openssl rand -hex 64)
    echo "Rotating NEXTAUTH_SECRET ..."
    az containerapp secret set \
        --name "${UI_APP_NAME}" \
        --resource-group "${RG}" \
        --secrets "nextauth-secret=${NEW_SECRET}" \
        --output none
fi

echo "Updating UI Container App image to ${UI_FULL_IMAGE} ..."
az containerapp update \
    --name "${UI_APP_NAME}" \
    --resource-group "${RG}" \
    --image "${UI_FULL_IMAGE}" \
    --output none

UI_FQDN=$(az containerapp show --name "${UI_APP_NAME}" --resource-group "${RG}" \
    --query properties.configuration.ingress.fqdn -o tsv)

echo
echo "── UI REDEPLOY COMPLETE ─────────────────────────────────────"
echo "UI URL       : https://${UI_FQDN}"
echo "UI sign-in   : https://${UI_FQDN}/login"
echo "Tail UI logs : az containerapp logs show -n ${UI_APP_NAME} -g ${RG} --follow"
echo "─────────────────────────────────────────────────────────────"
