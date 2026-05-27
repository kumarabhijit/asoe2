# GA preconditions — items deferred from PARITY (preprod) to GA

**Status:** Tracking list (created 2026-05-27). Updated whenever a
PARITY ticket lands an explicit "GA follow-up" note.
**Owner:** Platform + Security + Compliance (joint).
**Scope:** Capabilities the parity work intentionally postpones to GA,
with the rationale, the owning phase, and the gating event that
unblocks each item.

This document is the source of truth a GA-readiness review reads to
confirm every preprod-acceptable deferral has either been resolved or
explicitly re-blessed.

---

## Why preprod-vs-GA split exists

Preprod runs synthetic / internal data and is operated by the platform
team. GA runs real customer data under contracted SLAs. The split lets
us ship the parity substrate (Phase 0–8) on a preprod timeline without
the longer lead times of customer-facing rotation policies, HSM
procurement, or multi-tenant Entra negotiations.

---

## Deferred items

| # | Capability | Defer rationale | Owning phase | Unblock event |
|---|---|---|---|---|
| 1 | **Automated Key Vault secret rotation** (90-day policy via Azure Function callback + Container App revision restart) | Decision Q4: preprod uses operator-triggered rotation (`docs/ops/secrets-rotation.md`). The function callback adds infra surface; we want a clean preprod operator runbook first. | PARITY-4 → GA-4 | First production tenant onboarded; SOC 2 / ISO control mandates automated rotation. |
| 2 | **Multi-tenant Entra App Registration** (`signInAudience: AzureADMultipleOrgs` + per-tenant consent flow + cross-tenant `iss` validation table) | Decision Q1: preprod has one customer-equivalent — single-tenant is simpler. A documented migration path exists; the boundary doesn't have to be torn down at GA. | PARITY-3b → GA-3 | Second customer onboarded OR design-partner agreement signed. |
| 3 | **Asymmetric attachment signing key** (ES256 or EdDSA for `ASOE_ATTACHMENT_SIGNING_KEY`) | PARITY-4 ships separate HS256 keys (JWT vs attachment) with overlap rotation. Asymmetric keys make rotation transparent to verifiers but add a JWKS-style key-distribution surface we don't need in preprod. | PARITY-4 → GA-4 | First customer contract requires verifier independence (most common driver: a partner needs to verify capability URLs without our HS256 secret). |
| 4 | **Per-tenant attachment-byte residency + GRS** (per-tenant region selection + geo-redundant storage with tombstone-replication discipline) | Decisions Q6+Q7: preprod is single-region East US 2, LRS. GA needs per-tenant region pinning and GRS with the Compliance-reviewed tombstone-replication story so an erasure in region A is also reflected in region B's replica. | PARITY-2 / PARITY-8 → GA-2 | First customer with a DPA-driven residency commitment (EU, ANZ, or Canada most likely). |
| 5 | **Customer-managed HSM for the attachment signing key** (or per-tenant CMK on Storage + Key Vault) | Preprod uses Microsoft-managed encryption + Key Vault soft-key keys. GA's larger contracts often require customer-key separation either for regulatory (financial services) or contractual (right-to-revoke-access) reasons. | PARITY-4 → GA-4 | First customer contract requiring BYOK / HSM separation. |
| 6 | **Hot-reload of Key Vault secrets without revision restart** (Azure Function callback → Container App restart-free reload) | Preprod accepts a revision restart on rotation (acceptable cold-start window). GA tenants with strict SLAs may not. | PARITY-4 → GA-4 | First customer SLA below the cold-start budget (~30s). |
| 7 | **Multi-region active-active for the Container App** (or active-passive with regional failover) | Preprod is single-region — the Container Apps environment fails over only within East US 2. GA-grade SLAs require regional failover. | New GA phase (out of PARITY scope) | First customer SLA requiring regional RTO/RPO. |
| 8 | **Automated DPIA gate enforcement** (CI block on `tenant_id` first-seen if `compliance/dpia/{tenant-id}.md` is missing) | PARITY-8 ships the DPIA template + a manual gate. The CI block (refusing a new tenant id at deploy-time until the DPIA file exists) requires the tenant-id catalog to land first. | PARITY-8 → GA-8 | Tenant-config gateway pull (or a static `compliance/tenants.yaml`) lands; we know the closed set of tenant ids at deploy time. |
| 9 | **PII-detection on egress to App Insights** (today the log redactor handles known PII shapes; a learned model would catch the long tail) | PARITY-5's `api/observability/log_redaction.py` covers SSN / CC / IBAN / email / dollar amounts. The long tail (free-text descriptions referencing a customer, partial-PII fragments) is unscrubbed. | PARITY-5 → GA-5 | First customer audit finding OR a redaction-bypass discovered in penetration testing. |
| 10 | **Real-time DLQ → operator dashboard** (today `api/dead_letter_queue.list_for_tenant` is the read API; the UI surface is a follow-up) | PARITY-6 ships the in-process DLQ; the operator dashboard is a UI deliverable that depends on the live connector traffic patterns (which DLQ entries to surface most prominently). | PARITY-6 → GA-6 | First live connector saturates the in-memory DLQ OR the operator team requests the dashboard. |
| 11 | **Schema-evolution drift CI** (nightly fixture-vs-live diff → CI alert on drift) | `docs/ops/fixture-capture.md` documents the cadence; the automated CI alert needs the nightly `-m live` gate stable across the four real connectors first. | PARITY-6 → GA-6 | All four PARITY-6 sub-phases (Graph, DocAI, SAP, OMS) green on nightly for 7 consecutive days. |
| 12 | **Customer-facing erasure-certificate UI** (today `/attachments/{id}/erasure-certificate` is the API; tenant-facing download is a UI follow-up) | The endpoint is manager+admin only, tenant-scoped (PARITY-0.5). A customer-facing surface needs the customer-tenant authentication path (out of preprod scope — preprod tenants are internal). | PARITY-8 + UI → GA-8 | First customer in self-serve preview OR first regulator request for proof-of-erasure. |
| 13 | **Per-tenant retention TTL configuration from a runtime store** (today `_TENANT_RETENTION_TTL_DAYS` is module-level) | Preprod sets per-tenant TTL at deploy time. GA needs a runtime store (likely the tenant-config gateway) so an operator can adjust without a deploy. | PARITY-8 → GA-8 | First customer with a TTL renegotiation request OR the tenant-config gateway lands. |

---

## How an item leaves this list

A deferred item is removed (not struck through) when:

  1. A dedicated `docs/plans/ga-<phase>-<topic>.md` ADR ships with the
     implementation plan.
  2. The implementation lands behind an env flag (clearing the flag
     reverts to preprod behaviour, per `docs/plans/azure-preprod-parity-plan.md`
     §"Rollback / safety net").
  3. The acceptance criteria in this row are met (typically: regression
     test + operator runbook update).

If a row's "Unblock event" fires before the row's planned phase, the
row promotes to a P0 ticket on the relevant track and the rationale is
amended here with the new sequencing.

---

## Out-of-scope (this document)

* GA-phase capabilities that have never been on a PARITY ticket
  (multi-region active-active, customer-managed HSMs, BYOK) — these
  belong in a separate `docs/plans/ga-roadmap.md` when one lands.
* Per-customer contractual items — these belong in the customer's
  DPA / MSA, not in a platform-wide doc.
