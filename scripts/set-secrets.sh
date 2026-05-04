#!/usr/bin/env bash
# set-secrets.sh ─ Rotate ANTHROPIC_API_KEY, ASOE_JWT_SECRET, and/or the
# LangFuse Cloud key pair on the running Container App without re-deploying
# infra.
#
# DATABASE_URL and REDIS_URL are owned by deploy-azure.sh (derived from
# infra outputs there); this helper is intentionally small. If you need
# to rotate the Postgres password or Redis key, run the full deploy:
#   PG_ADMIN_PASSWORD='<new>' ./scripts/deploy-azure.sh
#
# Usage (set one or more):
#
#   ANTHROPIC_API_KEY='sk-ant-NEW' ./scripts/set-secrets.sh
#   ASOE_JWT_SECRET=auto ./scripts/set-secrets.sh        # generate fresh
#   LANGFUSE_PUBLIC_KEY='pk-lf-...' LANGFUSE_SECRET_KEY='sk-lf-...' \
#       ./scripts/set-secrets.sh
#
# After setting, the active revision is restarted so the new values are
# picked up by the running container.

set -euo pipefail

: "${RG:=asoepreprod}"
: "${NAME_PREFIX:=asoepreprod}"
: "${APP_NAME:=${NAME_PREFIX}api}"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ASOE_JWT_SECRET:-}" \
   && -z "${LANGFUSE_PUBLIC_KEY:-}" && -z "${LANGFUSE_SECRET_KEY:-}" ]]; then
    echo "ERROR: pass at least one of ANTHROPIC_API_KEY, ASOE_JWT_SECRET," >&2
    echo "       LANGFUSE_PUBLIC_KEY, or LANGFUSE_SECRET_KEY." >&2
    echo "       Example:" >&2
    echo "         ANTHROPIC_API_KEY='sk-ant-NEW' ./scripts/set-secrets.sh" >&2
    echo "         LANGFUSE_PUBLIC_KEY='pk-lf-...' LANGFUSE_SECRET_KEY='sk-lf-...' \\" >&2
    echo "             ./scripts/set-secrets.sh" >&2
    exit 1
fi

if [[ "${ASOE_JWT_SECRET:-}" == "auto" ]]; then
    ASOE_JWT_SECRET=$(openssl rand -hex 64)
    echo "Generated new ASOE_JWT_SECRET. Save this — it invalidates issued tokens:"
    echo "  ASOE_JWT_SECRET=${ASOE_JWT_SECRET}"
fi

# LangFuse keys are paired — refuse to set just one of the two, since
# the sink only activates when both are present.
if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" && -z "${LANGFUSE_SECRET_KEY:-}" ]] \
|| [[ -z "${LANGFUSE_PUBLIC_KEY:-}" && -n "${LANGFUSE_SECRET_KEY:-}" ]]; then
    echo "ERROR: LangFuse keys must be rotated as a pair (pass both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)." >&2
    exit 1
fi

# Build the --secrets list incrementally so we only touch what was passed.
secret_args=()
[[ -n "${ANTHROPIC_API_KEY:-}"    ]] && secret_args+=("anthropic-api-key=${ANTHROPIC_API_KEY}")
[[ -n "${ASOE_JWT_SECRET:-}"      ]] && secret_args+=("asoe-jwt-secret=${ASOE_JWT_SECRET}")
[[ -n "${LANGFUSE_PUBLIC_KEY:-}"  ]] && secret_args+=("langfuse-public-key=${LANGFUSE_PUBLIC_KEY}")
[[ -n "${LANGFUSE_SECRET_KEY:-}"  ]] && secret_args+=("langfuse-secret-key=${LANGFUSE_SECRET_KEY}")

echo "Setting ${#secret_args[@]} secret(s) on Container App ${APP_NAME} ..."
az containerapp secret set \
    --name "${APP_NAME}" \
    --resource-group "${RG}" \
    --secrets "${secret_args[@]}" \
    --output none

echo "Restarting the active revision so the new secret values are picked up ..."
REV=$(az containerapp revision list \
    --name "${APP_NAME}" --resource-group "${RG}" \
    --query "[?properties.active].name | [0]" -o tsv)
az containerapp revision restart \
    --revision "${REV}" \
    --name "${APP_NAME}" \
    --resource-group "${RG}" \
    --output none

FQDN=$(az containerapp show --name "${APP_NAME}" --resource-group "${RG}" \
    --query properties.configuration.ingress.fqdn -o tsv)

echo
echo "── SECRET(S) UPDATED ───────────────────────────────────────"
echo "Verify with:  curl -fsS --max-time 30 https://${FQDN}/api/v1/health | jq ."
echo "Tail logs:    az containerapp logs show -n ${APP_NAME} -g ${RG} --follow"
echo "────────────────────────────────────────────────────────────"
