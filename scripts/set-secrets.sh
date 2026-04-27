#!/usr/bin/env bash
# set-secrets.sh ─ Populate Container App secrets after the first bicep deploy.
#
# bicep declares the secret slots (anthropic-api-key, asoe-jwt-secret,
# database-url, redis-url) but leaves them empty. This script:
#   1. Reads the secret values from your environment (or prompts if missing).
#   2. Looks up the Postgres + Redis connection details from Azure.
#   3. Sets the four secrets on the Container App and rolls a new revision.
#
# Required env vars (or interactive prompts if unset):
#   ANTHROPIC_API_KEY       Anthropic API key (sk-ant-…)
#   ASOE_JWT_SECRET         Random 64-byte hex string (or 'auto' to generate)
#   PG_ADMIN_PASSWORD       Postgres admin password used in deploy-azure.sh
#
# Optional overrides (defaults match deploy-azure.sh):
#   RG, NAME_PREFIX
#
# Usage:
#   ANTHROPIC_API_KEY=sk-ant-... \
#   ASOE_JWT_SECRET=auto \
#   PG_ADMIN_PASSWORD='your-pg-pw' \
#       ./scripts/set-secrets.sh

set -euo pipefail

: "${RG:=asoepreprod}"
: "${NAME_PREFIX:=asoepreprod}"
: "${APP_NAME:=${NAME_PREFIX}api}"
: "${PG_SERVER:=${NAME_PREFIX}pg}"
: "${PG_DB:=asoe}"
: "${PG_USER:=asoeadmin}"
: "${REDIS_NAME:=${NAME_PREFIX}redis}"

prompt() {
    local var="$1" msg="$2" silent="${3:-0}"
    if [[ -z "${!var:-}" ]]; then
        if [[ "${silent}" == "1" ]]; then
            read -rsp "${msg}: " v; echo
        else
            read -rp "${msg}: " v
        fi
        printf -v "${var}" '%s' "${v}"
    fi
}

prompt ANTHROPIC_API_KEY "Anthropic API key (sk-ant-...)" 1
prompt ASOE_JWT_SECRET   "ASOE_JWT_SECRET (or 'auto' to generate)" 1
prompt PG_ADMIN_PASSWORD "Postgres admin password" 1

if [[ "${ASOE_JWT_SECRET}" == "auto" ]]; then
    ASOE_JWT_SECRET=$(openssl rand -hex 64)
    echo "Generated ASOE_JWT_SECRET ($(echo -n "${ASOE_JWT_SECRET}" | wc -c) chars). Save this — you'll need it for token signing parity across replicas."
    echo "  ASOE_JWT_SECRET=${ASOE_JWT_SECRET}"
fi

# ──────────────────────────────────────── Look up Postgres & Redis details

PG_HOST=$(az postgres flexible-server show \
    --name "${PG_SERVER}" --resource-group "${RG}" \
    --query fullyQualifiedDomainName -o tsv)

# Azure Managed Redis (Microsoft.Cache/redisEnterprise) — replaces the
# retiring Azure Cache for Redis. The CLI command is `az redisenterprise`
# (one word) and the connection port is 10000, not 6380.
az extension add --name redisenterprise --upgrade --yes >/dev/null 2>&1 || true

REDIS_HOST=$(az redisenterprise show \
    --name "${REDIS_NAME}" --resource-group "${RG}" \
    --query hostName -o tsv)

REDIS_KEY=$(az redisenterprise database list-keys \
    --cluster-name "${REDIS_NAME}" --resource-group "${RG}" \
    --query primaryKey -o tsv)

# URL-encode anything we splice into a connection-string user-info field.
# Without this, a Postgres password containing '@' (e.g. 'Foo@2026') makes
# psycopg2 split at the first '@' so the host becomes '2026@...' and DNS
# fails. Redis primary keys are base64 and can contain '+' '/' '='.
url_encode() {
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

PG_PASS_ENC=$(url_encode "${PG_ADMIN_PASSWORD}")
REDIS_KEY_ENC=$(url_encode "${REDIS_KEY}")

# psycopg2 + asyncpg both accept this URL form.
DATABASE_URL="postgresql://${PG_USER}:${PG_PASS_ENC}@${PG_HOST}:5432/${PG_DB}?sslmode=require"

# rediss:// = TLS; Managed Redis listens on 10000 and is always TLS.
# No /<db-number> suffix — Enterprise cluster mode supports a single
# logical database (the 'default' DB created by the bicep template).
REDIS_URL="rediss://:${REDIS_KEY_ENC}@${REDIS_HOST}:10000"

# ──────────────────────────────────────── Set secrets on the Container App

echo "Setting Container App secrets on ${APP_NAME} ..."
az containerapp secret set \
    --name "${APP_NAME}" \
    --resource-group "${RG}" \
    --secrets \
        "anthropic-api-key=${ANTHROPIC_API_KEY}" \
        "asoe-jwt-secret=${ASOE_JWT_SECRET}" \
        "database-url=${DATABASE_URL}" \
        "redis-url=${REDIS_URL}" \
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
echo "── SECRETS SET ─────────────────────────────────────────────"
echo "Verify with:  curl -fsS https://${FQDN}/api/v1/health | jq ."
echo "Tail logs:    az containerapp logs show -n ${APP_NAME} -g ${RG} --follow"
echo "────────────────────────────────────────────────────────────"
