# ADR-040: Four-Eyes / Cosign Migration to the Case Lifecycle

**Status:** Proposed
**Date:** 2026-05-09
**Deciders:** Compliance Veto Holder; Principal AI/Agentic Engineering Architect; Backend Engineering lead.
**Applies to:** `contracts/models.py::OrderCase`, `api/store.py::CaseStore`, `api/routes/cases.py`, `compliance/audit_bearing_registry.yaml`.
**Related:** ADR-038 (case-centric order intake — defines `OrderCase` parent), ADR-029 (override merge policy on the recipe level), the existing exception-level cosign flow at `api/routes/exceptions.py::cosign_override`.

---

## 1. Context

The four-eyes / cosign control today operates **per-exception**:
`ExceptionRecord.lifecycle_state == "PENDING_COSIGN"` after a high-value
override is initiated; the cosign endpoint at
`POST /api/v1/exceptions/{id}/override/cosign` lifts a single record
into RESOLVED with a SoD-safe second reviewer.

ADR-038 introduces `OrderCase` as the parent entity. A single case can
carry N child exception records (one per line item or per agent step
that escalated). Today an operator who decides to override at the case
level has to walk every child individually — N override calls, N cosign
calls, N audit events. This:

* **Splits the audit trail.** A single business decision ("approve
  this case despite the LLM downgrade") fragments into N records.
* **Multiplies SoD friction.** A different cosigner is required per
  exception, even when the underlying decision is the same.
* **Misses the case as the unit of authorisation.** ADR-038 §6.1
  treats the case as the SOX-relevant decision boundary; the cosign
  control should match.

## 2. Decision

**Promote four-eyes / cosign authority from the exception lifecycle to
the case lifecycle.** When an operator overrides at the case level
(via a forthcoming `POST /api/v1/cases/{id}/override` endpoint, NOT in
this ADR), the case carries a single `pending_override` record. A
single cosign call approves the override across every child exception.

The exception-level flow is **preserved unchanged** — operators who
override a single record still go through the existing
`api/routes/exceptions.py::cosign_override` path. The case-level
flow is additive.

### 2.1 Truth table (binding)

| Trigger                                                | Lifecycle state            | Endpoint                                       |
|--------------------------------------------------------|----------------------------|------------------------------------------------|
| Operator overrides one exception at high value         | exception → PENDING_COSIGN | existing `/exceptions/{id}/override/cosign`    |
| Operator overrides one exception at low value          | exception → RESOLVED       | n/a (immediate)                                |
| Operator overrides the **case** at high value          | case → PENDING_COSIGN      | NEW `/cases/{id}/override/cosign`              |
| Operator overrides the **case** at low value           | case → RESOLVED            | n/a (immediate)                                |

The threshold is the same `HIGH_VALUE_OVERRIDE_THRESHOLD_USD` (currently
$10k); the case-level financial-impact is the SUM of every child
exception's impact.

### 2.2 SoD invariants

The case-level cosign uses the **same** SoD rules as the exception-level
flow:

* The cosigner must not be the override initiator.
* The cosigner must hold `manager` or `admin` role.
* Notes are mandatory (SOX audit).

### 2.3 Audit-trail entries

* `CASE_OVERRIDE_INITIATED` — emitted when a case-level override
  enters PENDING_COSIGN. Captures `initiator`, `case_id`,
  `pending_action`, `aggregate_financial_impact_usd`.
* `CASE_OVERRIDE_COSIGNED` — emitted when a cosigner approves; carries
  `cosigned_by`, `cosigned_at`, `cosign_notes`, child_exception_ids
  promoted to RESOLVED.
* `CASE_OVERRIDE_REJECTED` — emitted when a cosigner rejects;
  reverts the case state and clears `pending_override`.

These three are NEW audit events, distinct from the existing
exception-level `EXCEPTION_OVERRIDE_*` family. Audit queries can
filter by event-type prefix to slice case-level vs exception-level
overrides.

## 3. Implementation phases

### 3.1 X.0 — Code path shipped, off by default (this commit)

* `OrderCase.pending_override: Optional[CasePendingOverride]` field
  added to the contract.
* `CaseStore.set_pending_override(...)` /
  `clear_pending_override(...)` helpers.
* `POST /api/v1/cases/{case_id}/override/cosign` endpoint behind
  `ASOE_CASE_COSIGN_ENABLED` env var (default off).
* Tests cover the new code path with the flag enabled.
* No change to the existing exception-level flow.

### 3.2 X.1 — Compliance ratification + flip

* Compliance workshop reviews the §2 truth table + SoD invariants.
* On ratification, `ASOE_CASE_COSIGN_ENABLED=1` is set in the live
  deployment's ConfigMap. No code change.

### 3.3 X.2 — UI surface (asoe-ui)

* `/cases/[id]` detail panel gets a `CaseOverrideAction` block when
  the case meets the high-value threshold.
* The cosign-pending banner the case detail renders mirrors the
  existing exception-detail "Pending Cosign" affordance.
* Companion to the asoe-ui /cases data-hook swap (Frontend Platform
  follow-up tracked separately).

### 3.4 X.3 — Sunset of per-exception override on cases that carry one

* Once X.2 ships, the operator's natural workflow lands on the case
  level. The exception-level override stays available but is
  effectively unused for case-attached records. We do **not** remove
  the per-exception path — solo records (Tier 1) still need it.

## 4. Open questions

* **Aggregate financial-impact computation.** Today's helper
  `_financial_impact_usd(record)` reads from `resolution_data`. The
  case-level aggregate sums the children's financial impacts. Edge
  case: a child whose `financial_impact_usd` is `None` (not measurable)
  doesn't move the aggregate. Compliance must ratify whether
  unmeasurable-impact children block the cosign path or are excluded
  from the aggregate. **Default in this ADR:** they're excluded; an
  unmeasurable child is treated as `0`.

* **Multi-pending-override cases.** The contract permits at most one
  `pending_override` per case (forward-only — initiated, then either
  cosigned or rejected). A second initiator hitting a case in
  `PENDING_COSIGN` returns 409 with a clear error. This matches the
  exception-level invariant.

* **Replay invariant.** Audit replay against a case must reconstruct
  the override → cosign sequence byte-identically given the same
  inputs (initiator, cosigner, notes, child_exception_ids, timestamp).
  The `case_events` table (V012) is the canonical source.

## 5. Definition of Done

ADR-040 is **Accepted** when:

* Compliance has signed off on §2 (truth table) and §2.2 (SoD).
* The X.0 code path is in place and tested (this commit).
* Compliance ratifies setting `ASOE_CASE_COSIGN_ENABLED=1` in the
  live deployment.
* No exception-level cosign behaviour has regressed.

---

*End of ADR-040.*
