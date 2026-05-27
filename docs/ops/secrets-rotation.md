# Secrets rotation — pre-prod runbook

**Status:** Operator-triggered (manual) per Decision Q4 of
`docs/plans/azure-preprod-parity-plan.md`. Automated 90-day rotation
via Azure Function callback is a GA follow-up (separate ADR).

This runbook covers the **pre-prod** deployment on Azure Container Apps
(`asoe2` API + `asoe-ui` Next.js). All secrets land in Key Vault
(once Phase 4 ships); until then they're Container App `@secure()` env
vars set via `scripts/set-secrets.sh`.

## What rotates

| Secret | Where it lives | Rotation impact |
|---|---|---|
| `ASOE_JWT_SECRET` | Container App env (Key Vault from PARITY-4) | Invalidates all active access + refresh tokens; users re-login |
| `ASOE_ATTACHMENT_SIGNING_KEY` | Container App env (Key Vault from PARITY-4) | Invalidates active attachment signed URLs (short-TTL, ~5min — low impact) |
| `ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY` | Container App env (Key Vault from PARITY-4) | Holds the previous attachment key during a rotation overlap window so in-flight mints still verify |
| `ANTHROPIC_API_KEY` (or other LLM provider) | Container App env (→ Key Vault after Phase 4) | First requests after restart re-authenticate; no in-flight impact |
| `DATABASE_URL` (password rotation) | Container App env (→ Key Vault) | Connection pool re-establishes; brief 5xx burst possible |
| `LANGFUSE_SECRET_KEY` | Container App env (→ Key Vault) | LangFuse forwarding pauses until restart |
| Postgres admin password | Postgres Flexible Server config | DBA-coordinated; not on this runbook |

## Pre-rotation checklist

1. **Off-peak window** — pick a low-traffic window (default: 22:00–06:00 UTC).
2. **Verify backups** — Postgres point-in-time-restore window is fine; no manual snapshot needed for secret rotation (it doesn't touch data).
3. **Coordinate operators** — post in `#asoe-ops` 30 minutes before; users will get logged out.
4. **Check active sessions** — `kubectl logs <api-pod> | grep "active_sessions"` or query Application Insights for `reviewer_decisions_total` last 15min to gauge user impact.

## Rotation procedure (pre-Phase-4)

```bash
# 1. Generate the new secret.
NEW_JWT_SECRET=$(openssl rand -base64 64 | tr -d '\n')

# 2. Update the Container App secret value.
az containerapp secret set \
  --name asoe-api \
  --resource-group asoe-preprod-rg \
  --secrets asoe-jwt-secret="$NEW_JWT_SECRET"

# 3. Trigger a new revision so the Container App picks up the new value.
az containerapp revision restart \
  --name asoe-api \
  --resource-group asoe-preprod-rg \
  --revision latest

# 4. Wait for health.
sleep 30
curl -fsS "https://$(az containerapp show -n asoe-api -g asoe-preprod-rg --query properties.configuration.ingress.fqdn -o tsv)/api/v1/health" \
  | jq -e '.status == "ok"'

# 5. Update the UI Container App if it consumes the same secret (NEXTAUTH_SECRET).
#    Repeat steps 2-4 for asoe-ui.
```

## Post-rotation verification

* **Login flow** — log in as `marcus.webb@acme-corp.com`; verify a new
  access token is issued and a /resolve call succeeds.
* **Existing tokens rejected** — confirm the previous access token is
  rejected (401 on /api/v1/me).
* **App Insights** — check the trace for the first `/api/auth/login`
  after restart; verify no 5xx burst.
* **Audit log** — record the rotation in the operator log
  (`docs/ops/rotation-history.md`, append-only):
  ```
  2026-MM-DD | <secret-name> | <operator> | <reason>
  ```

## Rotation procedure (post-Phase-4 — Key Vault references)

After Phase 4 ships, secrets become Key Vault references in the
Container App spec (`secretref:` syntax). The procedure simplifies:

```bash
# 1. Generate new secret + set it in Key Vault.
NEW_JWT_SECRET=$(openssl rand -base64 64 | tr -d '\n')
az keyvault secret set --vault-name asoe-preprod-kv \
  --name asoe-jwt-secret \
  --value "$NEW_JWT_SECRET"

# 2. Trigger a Container App revision restart so the secretref re-fetches.
az containerapp revision restart \
  --name asoe-api \
  --resource-group asoe-preprod-rg \
  --revision latest
```

Key Vault retains the previous secret value under
`enableSoftDelete: true` for 90 days. If the rotation produces
unexpected behaviour, recover with:

```bash
az keyvault secret recover --vault-name asoe-preprod-kv --name asoe-jwt-secret
az containerapp revision restart ...
```

### Attachment signing key rotation (zero-downtime overlap)

`ASOE_ATTACHMENT_SIGNING_KEY` is rotated independently of
`ASOE_JWT_SECRET` (PARITY-4). Because attachment-URL signatures are
short-TTL (~5min), the safe pattern is a small overlap window: move
the OLD key to the SECONDARY slot, mint new tokens under a fresh
PRIMARY, wait one TTL window, then clear the secondary.

```bash
# 1. Mint a new primary; remember the current one for the overlap.
OLD_KEY=$(az keyvault secret show --vault-name asoe-preprod-kv \
  --name asoe-attachment-signing-key --query value -o tsv)
NEW_KEY=$(openssl rand -base64 64 | tr -d '\n')

# 2. Move OLD to the SECONDARY slot, set NEW as PRIMARY.
az keyvault secret set --vault-name asoe-preprod-kv \
  --name asoe-attachment-signing-key-secondary --value "$OLD_KEY"
az keyvault secret set --vault-name asoe-preprod-kv \
  --name asoe-attachment-signing-key --value "$NEW_KEY"

# 3. Restart so the Container App picks up both secretrefs.
az containerapp revision restart -n asoe-api -g asoe-preprod-rg --revision latest

# 4. Wait for one ATTACHMENT_READ_URL_TTL_SECONDS window (default 300s)
#    plus a safety margin so any token minted just before the
#    rotation expires naturally.
sleep 600

# 5. Clear the secondary slot.
az keyvault secret set --vault-name asoe-preprod-kv \
  --name asoe-attachment-signing-key-secondary --value ""
az containerapp revision restart -n asoe-api -g asoe-preprod-rg --revision latest
```

The JWT secret is **never** rotated as part of this flow — they are
independently keyed by design, so an attachment-key compromise does
not require users to re-login.

### Key Vault break-glass recovery

If the vault itself is accidentally deleted (or the rotation policy
mis-fires and disables an active secret), purge-protection blocks the
60-day window before permanent destruction.

```bash
# Discover deleted vaults.
az keyvault list-deleted --query "[?name=='asoe-preprod-kv']"

# Recover the vault (restores all secrets).
az keyvault recover --name asoe-preprod-kv

# OR recover an individual secret without recovering the whole vault.
az keyvault secret list-deleted --vault-name asoe-preprod-kv
az keyvault secret recover --vault-name asoe-preprod-kv --name <secret-name>
```

Per Security review: the break-glass recovery key (used to bootstrap a
fresh vault if the primary is unrecoverable) MUST live in a separate
Key Vault under different RBAC, so a single compromised principal
cannot purge both.

## GA follow-up — automated rotation

Per Decision Q4, the GA target is a 90-day automated rotation policy:

* Key Vault rotation policy fires every 90 days.
* An Azure Function callback writes the new secret value (using
  managed identity → Key Vault).
* The function triggers a Container App revision restart so the new
  secret is picked up.
* The function emits an audit event into `policy_audit_log` (ADR-023
  immutable chain) — same provenance shape as a manual operator
  rotation.

Out of scope for this PARITY-0 cycle. Tracked in
`docs/plans/azure-preprod-parity-plan.md` Phase 4 as a deferred item.

## When NOT to rotate

* **During incident response** unless the secret is suspected leaked —
  rotation invalidates active sessions and may worsen the impact.
* **Before a scheduled deploy** — the deploy may restart the Container
  App anyway; coordinate.
* **Without coordinating with the asoe-ui deploy** if the same
  `NEXTAUTH_SECRET` is shared.

## Emergency rotation (secret leaked)

1. Treat as P1 incident; notify Security.
2. Rotate immediately following the procedure above.
3. Force-revoke all refresh tokens (Phase 3b adds this — until then,
   accept that any refresh token signed with the old secret stays
   valid until natural expiry; mitigate by short refresh TTL).
4. Audit `policy_audit_log` for any decisions made with a token issued
   under the leaked secret since suspected leak time.
5. Notify customers if regulatory disclosure threshold is hit.
