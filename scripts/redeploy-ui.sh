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
# IMAGE_TAG is intentionally NOT defaulted here. The default depends on
# the asoe-ui repo HEAD (not asoe2's), so we resolve it after the
# checkout sync below — once we know `${ASOE_UI_PATH}` is on the right
# branch and at the right commit. Defaulting from `git rev-parse HEAD`
# of the script's CWD (asoe2) was a real bug: a UI-only commit produced
# an image tag identical to the previously-deployed one, the
# `az containerapp update --image <unchanged>` call was a no-op, and
# the running revision kept serving the stale code.
: "${ASOE_UI_PATH:=../asoe-ui}"

command -v az >/dev/null || { echo "az CLI not found"; exit 1; }
az account show >/dev/null 2>&1 || { echo "Run 'az login' first"; exit 1; }
az account set --subscription "${SUBSCRIPTION_ID}"

: "${ASOE_UI_REPO_URL:=https://github.com/kumarabhijit/asoe-ui.git}"
# Remote branch on origin (the ref the script fetches from GitHub).
# Override for feature branches:
#   ASOE_UI_BRANCH=main ./scripts/redeploy-ui.sh
: "${ASOE_UI_BRANCH:=claude/dag-pipeline-representation-LQyx0}"
# Local checkout branch name. Defaults to a tidier hand-friendly name
# than the auto-generated remote ref so a developer pulling against the
# checkout sees the local branch they expect. Set ASOE_UI_BRANCH_LOCAL
# explicitly when the remote you point at via ASOE_UI_BRANCH is named
# the same as the local branch you want (e.g. ASOE_UI_BRANCH=main with
# ASOE_UI_BRANCH_LOCAL=main).
: "${ASOE_UI_BRANCH_LOCAL:=dag-pipeline-representation}"

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
        echo "Cloning asoe-ui (remote=${ASOE_UI_BRANCH} local=${ASOE_UI_BRANCH_LOCAL}) into ${ASOE_UI_PATH} (using PAT) ..."
        git clone --branch "${ASOE_UI_BRANCH}" --depth 1 \
            "${authed_url}" "${ASOE_UI_PATH}"
        git -C "${ASOE_UI_PATH}" remote set-url origin "${ASOE_UI_REPO_URL}"
    else
        echo "Cloning asoe-ui (remote=${ASOE_UI_BRANCH} local=${ASOE_UI_BRANCH_LOCAL}) into ${ASOE_UI_PATH} ..."
        if ! git clone --branch "${ASOE_UI_BRANCH}" --depth 1 \
            "${ASOE_UI_REPO_URL}" "${ASOE_UI_PATH}" 2>/dev/null; then
            echo "ERROR: clone failed. asoe-ui is private — set a PAT in one of:" >&2
            echo "       GITHUB_TOKEN | GH_TOKEN | GITHUB_CODESPACE_ACCESS" >&2
            exit 1
        fi
    fi

    # `git clone --branch X` lands on a local branch named X. Rename
    # so the developer's working tree carries ASOE_UI_BRANCH_LOCAL.
    if [[ "${ASOE_UI_BRANCH_LOCAL}" != "${ASOE_UI_BRANCH}" ]]; then
        git -C "${ASOE_UI_PATH}" branch -m "${ASOE_UI_BRANCH_LOCAL}"
        git -C "${ASOE_UI_PATH}" branch \
            --set-upstream-to="origin/${ASOE_UI_BRANCH}" \
            "${ASOE_UI_BRANCH_LOCAL}" >/dev/null 2>&1 || true
    fi
else
    # Existing checkout — make sure the local branch
    # (ASOE_UI_BRANCH_LOCAL) is checked out, tracking
    # origin/${ASOE_UI_BRANCH}, and reset to that ref. Fetches against
    # an in-URL-authed remote so we get GitHub's tested git-HTTPS auth
    # path (the `extraHeader: bearer` form is unreliable for
    # git-over-HTTPS — see deploy-azure.sh's sync_asoe_ui_to_branch
    # for the rationale).
    fetch_url="${ASOE_UI_REPO_URL}"
    if [[ -n "${GH_PAT}" ]]; then
        fetch_url="${ASOE_UI_REPO_URL/https:\/\//https://x-access-token:${GH_PAT}@}"
    fi

    current_branch=$(git -C "${ASOE_UI_PATH}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [[ "${current_branch}" != "${ASOE_UI_BRANCH_LOCAL}" ]]; then
        echo "asoe-ui is on '${current_branch}'; switching to '${ASOE_UI_BRANCH_LOCAL}' (remote: ${ASOE_UI_BRANCH}) ..."
        git -C "${ASOE_UI_PATH}" fetch "${fetch_url}" \
            "+refs/heads/${ASOE_UI_BRANCH}:refs/remotes/origin/${ASOE_UI_BRANCH}"
        git -C "${ASOE_UI_PATH}" checkout -B "${ASOE_UI_BRANCH_LOCAL}" "origin/${ASOE_UI_BRANCH}"
    else
        echo "asoe-ui is on '${ASOE_UI_BRANCH_LOCAL}'; fetching latest from origin/${ASOE_UI_BRANCH} ..."
        git -C "${ASOE_UI_PATH}" fetch "${fetch_url}" \
            "+refs/heads/${ASOE_UI_BRANCH}:refs/remotes/origin/${ASOE_UI_BRANCH}"
        git -C "${ASOE_UI_PATH}" reset --hard "origin/${ASOE_UI_BRANCH}"
    fi
fi

actual_branch=$(git -C "${ASOE_UI_PATH}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "<unknown>")
actual_sha=$(git -C "${ASOE_UI_PATH}" rev-parse --short HEAD 2>/dev/null || echo "<unknown>")
echo "asoe-ui checkout: branch=${actual_branch} sha=${actual_sha}"

# Now that we know the asoe-ui HEAD, derive IMAGE_TAG from it (unless
# the caller passed an override). Using the asoe-ui sha guarantees the
# tag changes whenever the UI source changes, which guarantees the
# subsequent `az containerapp update --image` call carries a different
# image reference and therefore creates a new Container App revision
# that picks up the rebuilt content.
: "${IMAGE_TAG:=${actual_sha}}"

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
        --build-arg "NEXT_PUBLIC_SHOW_PREVIEW_FEATURES=true" \
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

# Safety belt: if the image reference happened to match what was already
# running (e.g. a re-run with no source change), `az containerapp update`
# above is a no-op — no new revision, the running pod keeps serving its
# cached layers. Force a restart of the active revision so it re-pulls
# from ACR. Cheap when not needed (~5s); essential when it is.
ACTIVE_REV=$(az containerapp revision list \
    -n "${UI_APP_NAME}" -g "${RG}" \
    --query "[?properties.active].name | [0]" -o tsv 2>/dev/null || true)
if [[ -n "${ACTIVE_REV}" ]]; then
    echo "Restarting active revision ${ACTIVE_REV} so the new image is picked up ..."
    az containerapp revision restart \
        --revision "${ACTIVE_REV}" \
        --name "${UI_APP_NAME}" \
        --resource-group "${RG}" \
        --output none || true
fi

UI_FQDN=$(az containerapp show --name "${UI_APP_NAME}" --resource-group "${RG}" \
    --query properties.configuration.ingress.fqdn -o tsv)

# Surface the now-active revision + image so the operator can verify
# the rollout actually flipped. The image column in particular is the
# canonical confirmation that the new tag is being served.
echo
echo "── UI REDEPLOY COMPLETE ─────────────────────────────────────"
echo "UI URL       : https://${UI_FQDN}"
echo "UI sign-in   : https://${UI_FQDN}/login"
echo "asoe-ui src  : branch=${actual_branch} sha=${actual_sha}"
echo "Image tag    : ${IMAGE_TAG}"
echo "Active revision (image column should match the tag above):"
az containerapp revision list \
    -n "${UI_APP_NAME}" -g "${RG}" \
    --query "[?properties.active].{name:name, image:properties.template.containers[0].image, healthState:properties.healthState}" \
    -o table
echo
echo "Tail UI logs : az containerapp logs show -n ${UI_APP_NAME} -g ${RG} --follow"
echo "─────────────────────────────────────────────────────────────"
