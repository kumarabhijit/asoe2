# STATUS_MODEL.md — ASOE status & state surfaces

**Audience:** engineers and auditors.
**Status:** authoritative reference for the status/state fields ASOE
carries. Derived from the code — `contracts/models.py`,
`api/case_resolver.py`, `api/routes/exceptions.py`.

ASOE tracks an Order-to-Cash exception through several *distinct*
status surfaces. They are easy to conflate because several share value
names (`RESOLVED`, `BLOCKED`, `FAILED` appear in three different
enums). This document is the single place that enumerates every
surface, its allowed values, who writes it, and how the surfaces lead
into one another.

---

## 1. The seven status surfaces

| # | Surface | Field / type | Scope | Set by |
|---|---|---|---|---|
| 1 | **Intent** | `Intent` enum | per-record | `classify` node (constrained LLM) |
| 2 | **Shadow verdict** | `ShadowStatus` enum | per-record | `shadow_audit` node (Compliance Shadow) |
| 3 | **Resolution status** | `final_status` (`TerminalStatus`) | per-record | pipeline terminal (`build_analysis`); `REJECTED` only via HITL |
| 4 | **Lifecycle state** | `lifecycle_state` (`LifecycleState` enum) | per-record | derived from `final_status` at persist, then mutated by HITL endpoints |
| 5 | **Disposition sub-type** | `sub_type` (derived, not persisted) | per HITL `/disposition` call | `disposition_exception` handler |
| 6 | **Case status** | `OrderCase.status` (`CaseStatus`) | per-case (roll-up of children) | `recompute_case_status` (`api/case_resolver.py`) |
| 7 | **Workflow result** | saga result status | per multi-step workflow | saga executor (`orchestration/`) |

`Intent` and `case_type` / `email_classification` (ADR-041) are
*classification* axes, not statuses — they describe *what kind* of
exception/case this is, not *where it is* in its lifecycle. Intent is
listed above only because it gates everything downstream.

### Allowed values

| Surface | Allowed values |
|---|---|
| **Intent** | `CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO`, `PRICE_HOLD_RELEASE`, `EDI_MISMATCH`, `BACK_ORDER`, `OVER_MAX`, `MIN_ORDER_QTY`, `PALLET_CONFIG`, `DELIVERY_DELAY`, `MANUAL_ORDER_INTAKE`, `UNKNOWN` |
| **Shadow verdict** | `GREEN`, `YELLOW`, `RED` |
| **Resolution status** (`final_status`) | `COMPLETE`, `COMPLETE_WITH_CHILDREN`, `MANUAL_REVIEW_REQUIRED`, `BLOCKED`, `FAIL_TO_HUMAN`, `AUDIT_CONTEXT_MISSING`, `REJECTED` |
| **Lifecycle state** | `INGESTED`, `CLASSIFYING`, `AUDITING`, `PENDING_REVIEW`, `ESCALATED`, `PENDING_ADMIN_REVIEW`, `PENDING_COSIGN`, `RESOLVED`, `FAILED`, `BLOCKED`, `REJECTED`, `CLOSED` (12-state) |
| **Disposition sub-type** | `APPROVE`, `REJECT`, `OVERRIDE` |
| **Case status** | `OPEN_AGENT_PROCESSING`, `OPEN_AWAITING_HUMAN`, `OPEN_AWAITING_BUYER`, `OPEN_AWAITING_ERP`, `RESOLVED`, `FAILED`, `BLOCKED` |
| **Workflow result** | `COMPLETE`, `FAILED`, `COMPENSATED`, `PARTIAL` |

---

## 2. The causal chain — what leads to what

```
Inbound event
   │
   ▼  classify node
[1] Intent ───────────────► selects Skill + Recipe
   │
   ▼  shadow_audit node
[2] Shadow verdict        GREEN          YELLOW            RED
   │                        │              │               │
   ▼  execute + build_analysis              │               │
[3] final_status        COMPLETE   MANUAL_REVIEW_REQUIRED  BLOCKED
   (TerminalStatus)   (or …_WITH_CHILDREN)  │       (FAIL_TO_HUMAN /
   │                        │              │        AUDIT_CONTEXT_MISSING
   │                        │              │        on a pipeline error)
   ▼  exception_store.create → STATUS_TO_LIFECYCLE
[4] lifecycle_state     RESOLVED      PENDING_REVIEW     BLOCKED / FAILED
   │                        │              │               │
   │          ┌─────────────┴─ HITL endpoints mutate it ────┴─────────┐
   │          ▼                                                       ▼
   │   /disposition · /override/cosign · /escalate · /reanalyze ·
   │   /challenge · /admin-release
   │          │
   ▼  recompute_case_status  (dominance roll-up over ALL child records)
[6] OrderCase.status
```

Surface 7 (workflow result) is *internal* to a single recipe's
multi-step saga — it never escapes onto the record; a failed saga
surfaces as `final_status = FAIL_TO_HUMAN`.

---

## 3. Deterministic derivation maps

These three maps are pure functions — same input, same output — so the
chain above is replayable for audit.

### 3.1 `final_status` → `lifecycle_state`

`STATUS_TO_LIFECYCLE` in `contracts/models.py`, applied once by
`exception_store.create` at persist time:

| `final_status` | → `lifecycle_state` |
|---|---|
| `COMPLETE`, `COMPLETE_WITH_CHILDREN` | `RESOLVED` |
| `MANUAL_REVIEW_REQUIRED` | `PENDING_REVIEW` |
| `BLOCKED` | `BLOCKED` |
| `FAIL_TO_HUMAN`, `AUDIT_CONTEXT_MISSING` | `FAILED` |
| `REJECTED` | `REJECTED` |

After this point the two fields **diverge**: HITL actions move
`lifecycle_state` but leave `final_status` as the original pipeline
verdict (except `/disposition` and `/reanalyze`, which rewrite both).

### 3.2 `lifecycle_state` → `CaseStatus` candidate

`api/case_resolver.py::_case_status_from_lifecycle` — one candidate per
child record:

| Child `lifecycle_state` | → `CaseStatus` candidate |
|---|---|
| `RESOLVED`, `CLOSED`, `REJECTED` | `RESOLVED` |
| `BLOCKED` | `BLOCKED` |
| `FAILED` | `FAILED` |
| `PENDING_REVIEW`, `ESCALATED`, `PENDING_ADMIN_REVIEW`, `PENDING_COSIGN`, `INGESTED`, `CLASSIFYING`, `AUDITING` | `OPEN_AWAITING_HUMAN` |

`REJECTED` projects to `RESOLVED` — a `NO_ACTION` disposition is a
*completed* human decision (`resolved_by` stamped, audited as
`EXCEPTION_RESOLVED`), so a rejected child is terminal-closed for the
roll-up and must not hold its case open.

### 3.3 candidates → one `CaseStatus`

`api/case_resolver.py::_aggregate_case_status` — dominance order, the
case sits at its *least-settled* child:

```
OPEN_AWAITING_HUMAN  >  BLOCKED  >  FAILED  >  RESOLVED
```

`OPEN_AGENT_PROCESSING` is the `OrderCase` default before any child
record exists. `OPEN_AWAITING_BUYER` / `OPEN_AWAITING_ERP` are valid
`CaseStatus` values set by other flows (a `REQUEST_CLARIFICATION` reply
path / an ERP submit) — they are **not** produced by the roll-up.

---

## 4. HITL transitions — how the operator moves `lifecycle_state`

Every endpoint below also triggers `recompute_case_status`
(`_reaggregate_parent_case` in `api/routes/exceptions.py`), so the case
status (surface 6) always reflects the child set.

| Endpoint | `sub_type` | `lifecycle_state`: from → to | `final_status` |
|---|---|---|---|
| `PATCH /exceptions/{id}/disposition` | `APPROVE` (chosen == recommended) | `PENDING_REVIEW` / `ESCALATED` / `PENDING_ADMIN_REVIEW` → `RESOLVED` | `COMPLETE` |
| `PATCH /exceptions/{id}/disposition` | `REJECT` (chosen == `NO_ACTION`) | same → `REJECTED` | `REJECTED` |
| `PATCH /exceptions/{id}/disposition` | `OVERRIDE` (chosen ≠ recommended) | same → `RESOLVED` (or → `PENDING_COSIGN` if high-value) | `COMPLETE` |
| `POST /exceptions/{id}/override/cosign` | — | `PENDING_COSIGN` → `RESOLVED` (approve) / prior state (reject) | `COMPLETE` on approve |
| `POST /exceptions/{id}/escalate` | — | `PENDING_REVIEW` / `FAILED` / `BLOCKED` → `ESCALATED` | unchanged |
| `POST /exceptions/{id}/challenge` | — | `RESOLVED` → `ESCALATED` | unchanged |
| `POST /exceptions/{id}/admin-release` | — | `BLOCKED` → `PENDING_ADMIN_REVIEW` | unchanged |
| `POST /exceptions/{id}/reanalyze` | — | re-runs the graph → new `final_status` → new `lifecycle_state` | recomputed |

`APPROVE` / `REJECT` / `OVERRIDE` are **not** separate endpoints — they
are the `sub_type` the `/disposition` handler derives from `chosen
action vs recommended action`. See `docs/AUDITOR_GUIDE.md` §10 / §18.

---

## 5. Key relationships & invariants

- **Intent is not a status.** It picks the deterministic recipe; it
  does not move through a lifecycle.
- **Shadow verdict decides the terminal outcome:** `GREEN` →
  `COMPLETE`, `YELLOW` → `MANUAL_REVIEW_REQUIRED`, `RED` → `BLOCKED`.
- **`final_status` → `lifecycle_state` is 1:1 at persist**, then the
  two diverge — `final_status` records *what the pipeline decided*,
  `lifecycle_state` records *where the record is now*.
- **`REJECTED` is the one human-only `final_status`.** The pipeline
  never emits it; only a `/disposition` `REJECT` (`action =
  NO_ACTION`) does.
- **A case is only as settled as its least-settled child.** One child
  awaiting a human holds the whole case at `OPEN_AWAITING_HUMAN`.
- **A case can reopen.** A terminal case (`RESOLVED` / `BLOCKED` /
  `FAILED`) that takes a new non-terminal child rolls back to a
  non-terminal status and `closed_at` is cleared.
- **Cosign-parked cases are exempt.** When `OrderCase.pending_override`
  is set, the cosign flow owns the status and the roll-up is skipped.

---

## 6. Source of truth

| Surface / map | Defined in |
|---|---|
| `Intent`, `ShadowStatus`, `TerminalStatus`, `LifecycleState` enums | `contracts/models.py` |
| `LIFECYCLE_STATES` (single-sourced from `LifecycleState`), `STATUS_TO_LIFECYCLE` | `contracts/models.py` |
| `CaseStatus`, `OrderCase` | `contracts/models.py` |
| `_case_status_from_lifecycle`, `_aggregate_case_status`, `recompute_case_status` | `api/case_resolver.py` |
| Disposition `sub_type` derivation, `_reaggregate_parent_case` | `api/routes/exceptions.py` |
| Case-status aggregation audit notes | `docs/AUDITOR_GUIDE.md` §19.7 |
| Workflow result statuses | `docs/AUDITOR_GUIDE.md` §5.2 |

`ARCHITECTURE.md` §7.1 gives an abstracted summary of the lifecycle;
this document is the code-accurate detail and supersedes it where they
differ (the authoritative lifecycle is the 12-state `LIFECYCLE_STATES`).

---

## 7. Keeping this document in sync

Update this file when any of the following change:

- a value is added to / removed from `Intent`, `ShadowStatus`,
  `TerminalStatus`, `CaseStatus`, or `LIFECYCLE_STATES`;
- `STATUS_TO_LIFECYCLE` or `_case_status_from_lifecycle` changes a
  mapping;
- the `_aggregate_case_status` dominance order changes;
- a HITL endpoint is added, or an existing one's lifecycle transition
  changes.

`prompts/update_docs.md` lists this file as a doc-sync target.
