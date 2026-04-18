# ADR-023: Unified `/disposition` primitive + hash-chained append-only audit log

**Status:** Accepted
**Date:** 2026-04-18
**Deciders:** Principal AI Systems Architect; Compliance Review Panel
**Applies to:** `api/routes/exceptions.py`, `api/schemas.py`,
`contracts/models.py`, `contracts/policy.py`, `constraints/specs.py`,
`db/repository.py`, `db/migrations/V003__audit_hash_chain.sql`,
`api/store.py`

---

## Context

The three-tier HITL surface that shipped with Phase 12 exposed three
per-verb endpoints — `PATCH /exceptions/{id}/override`,
`POST /exceptions/{id}/approve`, `POST /exceptions/{id}/reject` —
backed by three distinct audit events. A v3 compliance review of this
surface surfaced several problems:

1. **Inconsistent audit stream.** A question as simple as "how often do
   managers deviate from the agent's recommendation?" required a UNION
   across three event keys, with per-key field shapes. Downstream ML
   pipelines could not cluster dispositions without bespoke parsing.
2. **Trust-boundary defect.** `OverrideRequest.resolved_by` was a
   client-supplied string. A caller could spoof another auditor's
   identity on the override record — the JWT `sub` was present but not
   authoritative. SOX §302/§404 require that the auditor identity be
   system-derived.
3. **No tamper evidence on the audit log.** The V001 SOX immutability
   trigger rejected casual `UPDATE`/`DELETE`, but a privileged operator
   with `DROP TRIGGER` rights could mutate a row and re-install the
   trigger, leaving no visible trace. Reviewers could not *prove* a
   given audit row had neither been edited nor deleted after write.
4. **No Segregation of Duties.** A single manager who had previously
   resolved an exception could silently override their own decision on
   a later review. No system-enforced four-eyes existed for
   high-dollar overrides.
5. **No per-intent reason vocabulary.** Free-text `change_reason` was
   the only justification field. Clustering by category required NLP
   on arbitrary strings — again, no usable ML signal.
6. **Lifecycle drift.** `EXECUTING` existed only to stage the legacy
   `/approve` flow and was not aligned with any real business state.

A stakeholder-approved Option A overhaul was scoped across four
phases, converging on this decision record.

---

## Decision

### 1. Collapse the three HITL verbs under a single `/disposition` primitive

`PATCH /api/v1/exceptions/{id}/disposition` is the only endpoint that
resolves an exception. Request body:

```
{ action: str, notes: str, reason_tag: str }
```

The server derives `sub_type` from the pair
(`chosen_action`, `recommended_action`):

| chosen_action                         | derived `sub_type` | required permission       |
|---------------------------------------|--------------------|---------------------------|
| `"NO_ACTION"`                         | `REJECT`           | `exceptions:approve`      |
| equals `recommended_action`           | `APPROVE`          | `exceptions:approve`      |
| differs from `recommended_action`     | `OVERRIDE`         | `exceptions:override` (+ SoD + four-eyes) |

One audit event — `EXCEPTION_RESOLVED` — carries the derived
`sub_type` on `new_value`. A single SQL query answers the compliance
question; one event stream feeds the ML clustering job.

The legacy `/override`, `/approve`, and `/reject` endpoints and the
`OverrideRequest` / `ApproveRequest` / `RejectRequest` schemas are
**deleted** (Phase 3 — no backward-compat shim). `HITL_APPROVE_STATES`
+ `HITL_REJECT_STATES` collapse into a single `HITL_DISPOSITION_STATES`
(they had identical membership). The `EXECUTING` lifecycle state —
produced only by the deleted `/approve` — is dropped. The resulting
lifecycle is **12 states** (`contracts/models.py` → `LIFECYCLE_STATES`):
`INGESTED, CLASSIFYING, AUDITING, PENDING_REVIEW, ESCALATED,
PENDING_ADMIN_REVIEW, PENDING_COSIGN, RESOLVED, FAILED, BLOCKED,
REJECTED, CLOSED`.

### 2. Escalate and cosign are distinct primitives

Escalation and second-reviewer cosign are not dispositions; they carry
no resolution. Each has its own endpoint, permission, and audit event:

- `POST /exceptions/{id}/escalate` — routing only. Body
  `{ reason, to_role? }`. Permission `exceptions:escalate` (analyst+).
  Audit event `EXCEPTION_ESCALATED`.
- `POST /exceptions/{id}/override/cosign` — four-eyes second reviewer
  on a staged high-value override. Body `{ approve, notes }`.
  Permission `exceptions:override` (manager+). SoD enforced at the
  endpoint level (caller ≠ `pending_override.initiator`). Audit
  events `EXCEPTION_OVERRIDE_INITIATED` (staging),
  `EXCEPTION_OVERRIDE_COSIGNED` (applying), `EXCEPTION_OVERRIDE_REJECTED`
  (restoring the prior lifecycle).

### 3. Audit log is hash-chained at two layers

Every `policy_audit_log` row carries `prev_hash` + `event_hash` where
`event_hash = sha256(prev_hash || "|" || canonical_json(row))`. The
first row per tenant chains from the literal string `GENESIS`.

- **Application layer** (`api/store.py`, `db/repository.py`) computes
  and stores the hash on every INSERT using a shared `_audit_event_hash`
  helper whose canonical JSON output is locked in by a cross-layer
  parity test.
- **DB layer** (`db/migrations/V003__audit_hash_chain.sql`) adds the
  columns, backfills a valid chain across existing rows in
  `(tenant_id, created_at, id)` order, and installs `BEFORE UPDATE`
  and `BEFORE DELETE` triggers that raise
  `policy_audit_log is append-only; <OP> rejected (drop trigger to override)`.

`verify_audit_chain(tenant_id)` is exposed on both the in-memory store
and the repository. It returns `(True, None)` on a clean chain, or
`(False, first_break_idx)` pointing at the first row whose
recomputed hash no longer matches — whether by edit or by deletion of
a predecessor.

### 4. `reason_tag` is required; controlled vocabulary; per-intent framework

`DispositionRequest.reason_tag` is required on the wire (no default).
It is validated against `AllowedOverrideReasonTag` in
`constraints/specs.py`:

```
AllowedOverrideReasonTag = Literal[
    "customer_concession", "contract_stale", "data_error",
    "policy_exception", "agent_misclassification", "other",
]
```

`INTENT_REASON_TAGS` in the same module maps each `AllowedIntent` to
an allowed subset. Today every intent points at the full global set —
the **framework** is in place; **curation** is deferred to Phase 5
(data-only change, no schema/API/UI work). The mechanism is verified
by tests that monkey-patch a narrowed set.

### 5. `resolved_by` is never client-supplied

Every endpoint that writes `resolved_by` sets it from the JWT `user.sub`.
The `OverrideRequest.resolved_by` field was removed in Phase 19 — the
spoofing vector is no longer representable in the wire format.
Segregation of Duties is enforced on top of this: if the record's
prior `resolved_by` equals the caller's `user.sub` (and is not a
`system:*` principal), `/disposition` with `sub_type == OVERRIDE`
returns `403 SOD_VIOLATION`.

Four-eyes staging uses `HIGH_VALUE_OVERRIDE_THRESHOLD_USD` in
`contracts/policy.py` (default `10_000.0`). A disposition whose
derived `sub_type == OVERRIDE` and whose `financial_impact_usd`
meets or exceeds the threshold transitions the record to
`PENDING_COSIGN` and stashes the pending action on
`resolution_data.pending_override`. A different manager+ must cosign
before the action is applied.

---

## Consequences

### Positive

- **One audit query covers the whole disposition story.** Reporting
  on approve/reject/override rates reduces to a single `SELECT` over
  `EXCEPTION_RESOLVED.new_value.sub_type`.
- **Spoofing is no longer representable.** `resolved_by` leaves the
  wire contract; SoD enforces identity even inside the auditor pool.
- **Tamper evidence.** The V001 immutability trigger is still the
  first line of defence; the hash chain makes any successful
  trigger-drop + edit + re-install visible at verification time.
- **ML signal is ready.** Clustering by `(intent, reason_tag)` drops
  out of the audit log with no NLP step. Per-intent curation is a
  pure data change when stakeholders publish the categories.
- **Lifecycle is smaller and more honest.** 13 states → 12
  (`EXECUTING` was only ever produced by the deleted `/approve`).

### Negative

- **pgcrypto is required on PostgreSQL.** V003's backfill uses
  `digest()` from `pgcrypto`. The migration creates the extension
  when privileges permit and falls through with a `RAISE NOTICE`
  otherwise — new inserts still chain (Python-side); only the
  retroactive backfill is skipped.
- **Deleting three endpoints broke 70+ tests.** All were migrated to
  `/disposition` in the same change set; audit assertions swung from
  `EXCEPTION_OVERRIDE` to `EXCEPTION_RESOLVED + sub_type`.
- **In-memory idempotency cache.** The Phase 1 cache is per-process.
  A multi-replica deployment needs the Phase 3 Redis migration — the
  interface is already parameterised for a drop-in swap.

### Neutral

- **Challenge and admin-release kept as separate primitives.**
  Option A stakeholder guidance: these are re-open and unblock flows,
  not resolutions. They remain distinct endpoints with their own
  audit events.

---

## Alternatives Considered

1. **Keep the three verbs, add a common discriminator field.**
   Rejected: clients still had to implement three endpoints, the
   audit stream was still three event types, and the trust-boundary
   defect on `OverrideRequest.resolved_by` could not be closed without
   a wire-format break anyway.
2. **Application-layer hash chain only.** Rejected: an operator with
   direct DB access could mutate `policy_audit_log` without the
   application ever seeing it. The DB-layer triggers close that path.
3. **DB-layer hash chain only (triggers compute the hash).** Rejected:
   the application still reads back the log; dual computation with a
   parity test locks in identical canonical JSON across both paths
   and keeps the hash function visible in Python source.
4. **Deploy `/disposition` alongside the legacy endpoints with a
   deprecation window.** Rejected for the internal repo: the Phase 3
   scope included migrating 70+ test call sites, and keeping the
   legacy surface alive would have preserved the trust-boundary
   defect and the fragmented audit stream for the deprecation
   window. External API consumers do not exist yet; the cost of a
   hard cutover was zero.

---

## Compliance

This decision is referenced in:
- `README.md` (endpoint table, "Audit trail" subsection)
- `docs/AUDITOR_GUIDE.md` §10 (HITL audit events), §13.1.1
  (hash-chained append-only audit log), §18 (HITL governance controls)
- `tasks.md` Phase 19 and Phase 20
- `openapi/asoe2.openapi.json` (`/disposition`, `/escalate`,
  `/override/cosign`)

---

## Review Triggers

Re-evaluate this decision when any of the following occur:

- A second replica (or a second process) needs a consistent idempotency
  cache — migrate the in-memory TTL map to Redis.
- Stakeholders publish curated per-intent reason categories — replace
  the `_GLOBAL_REASON_TAGS` seeding in `constraints/specs.py` and
  regenerate the OpenAPI artifact.
- A regulated tenant requires an external signature (HSM-backed) on
  each audit event — extend `_audit_event_hash` to include a signature
  column and publish the verifier key.
- A breaking-change proposal emerges for the disposition event shape —
  the shape is a stable part of the audit contract; any change should
  add a new event, not mutate `EXCEPTION_RESOLVED`.
