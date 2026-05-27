# Erasure flows — operator runbook (PARITY-8)

> Two erasure modes; different operator UIs, different audit-event
> sub-types, and different downstream effects. Read the whole doc
> before triggering either.

## The two modes

### Mode A — erase-customer-data

**Scope:** the customer's `attachment_id` (bytes) AND the references
to that attachment in the audit chain are wiped. The tombstone in
`policy_audit_log` records the deletion happened, but no recoverable
artifact remains.

**Triggers:**
* GDPR Article 17 / equivalent (right to erasure).
* Customer offboarding (data retention scheduled to expire at
  contract end).

**Audit event:** `ATTACHMENT_ERASED` (PARITY-0.5 baseline).

**Operator UI:**
1. Tenant admin opens the case.
2. "Erase attachment" → confirm dialog → reason picker (GDPR / off-board /
   regulator).
3. Backend `gateways.attachment_store.erase_attachment` writes the
   tombstone BEFORE wiping the bytes (proof-of-erasure invariant).
4. Operator downloads the certificate from
   `GET /api/v1/attachments/{id}/erasure-certificate`.

### Mode B — erase-content-keep-attestation

**Scope:** the customer's bytes are wiped, but the `policy_audit_log`
chain entries that reference the attachment STAY — the attestation
that an action occurred (override approved, exception resolved) is
preserved for SOX evidence, even though the underlying bytes are gone.

**Triggers:**
* Storage-cost rationalisation past the legal retention window.
* Bulk archival of pre-contract data.

**Audit event:** `SCHEDULED_RETENTION_DELETE` (PARITY-8).

**Operator UI:**
1. Operator triggers a `RetentionSweeper.dry_run(tenant_id, as_of_unix)`.
2. The plan is reviewed (which attachments, which TTLs, which
   tenants).
3. Operator confirms; `commit_with_residency_check` enforces residency
   THEN wipes the bytes. The audit-chain entries that referenced the
   attachment remain valid (the hash chain isn't touched).
4. Aggregate certificate emitted listing the deleted set.

## Manual-replay refusal

Customer-disputed deletions do NOT restore from backup. The audit
chain proves the deletion happened at the claimed time; that's the
dispute response. Restoring bytes from a snapshot would defeat the
deletion's compliance claim AND introduce a recovery vector for
GDPR-erased data.

If a customer disputes a deletion:

1. Pull the erasure certificate via
   `GET /api/v1/attachments/{id}/erasure-certificate`.
2. The certificate carries: tombstone (PII-free) + audit-chain proof
   (sequence + event_hash + prior_hash) + the chain-verify result.
3. Hand the certificate to the customer / regulator.
4. Do NOT restore from backup. The certificate IS the response.

## Anchors

The pre-defined alerts in `api/observability/alerts.py` deep-link
here:

### zero-highlight {#zero-highlight}

Containment dropped to zero on a case that previously rendered
highlights. Investigate:
* AzureDI model_id drift — check `ASOE_DOCUMENT_EXTRACTION_MODEL_ID`
  against the pinned value.
* Verifier degraded to text mode — check the structured logs for
  `_normalize` mismatches (soft hyphen / ligature regression).

### layer2-open-rate {#layer2-open-rate}

Operator expansion rate deviates >2σ. Likely a UX regression where
Layer-1 stopped being self-explanatory. Roll back the most recent
analysis-composer change OR open an Ops ticket.

### breaker-open {#breaker-open}

A gateway breaker has been OPEN > 5 minutes. Pages on-call.
Investigate the DLQ via the operator dashboard
(`api.dead_letter_queue.list_for_tenant`).

### extraction-cost-overrun {#extraction-cost-overrun}

Per-page AzureDI cost > `EXTRACTION_MAX_COST_USD_PER_PAGE` on > 1% of
calls. Provider drift OR a misrouted custom-extract caller.

### audit-chain-verify-failed {#audit-chain-verify-failed}

P0 incident. Investigate immediately:
1. Stop all writes to `policy_audit_log` (Container App revision
   freeze).
2. Pull the most recent verifying entry.
3. Identify the corrupted entry (binary search on event_hash).
4. Coordinate with Compliance before any remediation.

### retention-sweeper-anomaly {#retention-sweeper-anomaly}

Sweeper delete count > 10× trailing 7d median OR attempted a
residency-violating delete. Sweeper auto-pauses. Investigate before
re-enabling:
1. Confirm the dry-run plan matched what was attempted.
2. Verify the residency-check log.
3. Page Compliance before flipping `RETENTION_SWEEPER_ENABLED=true`
   back on.

## Pre-rotation checklist for either mode

* Off-peak window where possible.
* DPIA on file for the affected tenant
  (`compliance/dpia/{tenant-id}.md`).
* Residency check confirmed (Mode B only).
* Operator identity resolved via
  `api.retention_sweeper.resolve_identity_for_sweep`.
