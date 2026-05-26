# DPIA — Tenant: `<tenant-id>`

> **PARITY-8 Compliance gate.** This record is REQUIRED before Phase 8
> retention enables on a tenant's real data. Copy this template to
> `compliance/dpia/<tenant-id>.md`, fill out every section, and obtain
> Compliance reviewer sign-off (CODEOWNERS-gated) BEFORE flipping
> `RETENTION_SWEEPER_ENABLED=true` for that tenant.

## Identification

| Field | Value |
|---|---|
| Tenant id | `<tenant-id>` |
| Customer name | `<…>` |
| Contract effective date | `<YYYY-MM-DD>` |
| DPA on file | Yes / No (link) |
| Reviewer (Compliance) | `<name>` |
| Reviewer sign-off date | `<YYYY-MM-DD>` |
| Review cadence | annually |

## Data scope

| Data class | Purpose | Legal basis | Retention TTL (days) |
|---|---|---|---|
| Inbound email body | Exception evidence | Legitimate interest (contract performance) | 365 |
| Attachment bytes (PDF / images) | Order extraction | Legitimate interest | 365 |
| Spatial extraction overlays | Operator decision support | Legitimate interest | 365 |
| Audit chain entries | SOX evidence | Legal obligation | 2555 (7y) |

## Residency

| Field | Value |
|---|---|
| Declared residency | e.g. `eu-west-1` |
| Storage region(s) in use | e.g. `eu-west-1`, `eu-central-1` |
| Cross-region replication | No (preprod) / GRS (GA) |
| Residency check active | Yes — `api/retention_sweeper.commit_with_residency_check` |

## Erasure rights

| Right | Implementation | Endpoint |
|---|---|---|
| Erasure of customer data | `gateways.attachment_store.erase_attachment` | `DELETE /api/v1/attachments/{id}` |
| Proof of erasure | Tombstone in `policy_audit_log` | `GET /api/v1/attachments/{id}/erasure-certificate` |
| Manual replay refusal | Documented in `docs/ops/erasure-flows.md` | n/a — by design, no replay from backup |

## Retention sweeper config

| Field | Value |
|---|---|
| `RETENTION_SWEEPER_ENABLED` | `false` (default) → set after sign-off |
| Default TTL (days) | 365 |
| Per-tenant override | record in `contracts.policy._TENANT_RETENTION_TTL_DAYS` |
| Sweep cron | Container Apps Job, weekly Mondays 02:00 UTC |
| Sweeper identity (audit) | `system:service-principal` (or operator JWT on manual dry-run) |

## DPIA risk register

| Risk | Mitigation | Owner |
|---|---|---|
| Sweeper deletes from wrong region | Residency check in `commit_with_residency_check` | Compliance |
| Sweeper deletes more than expected | Dry-run + operator confirm gate; `retention-sweeper-anomaly` alert | Operator |
| Audit chain breaks on bulk delete | `verify_audit_chain` post-sweep + Sev1 alert | Platform |
| Customer-disputed deletion claim | Erasure certificate is the dispute response; bytes NOT restored from backup | Compliance |
| TTL too short / too long | Annual review + Compliance sign-off | Compliance |

## Out of scope

* GA-only items: customer-managed HSM, per-tenant key residency,
  rolling decryption.
* The audit trail itself is exempt from this TTL — it must outlive
  the data it audits.

## Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Compliance reviewer | | | |
| Tenant admin (acknowledged) | | | |
| Platform engineering lead | | | |
