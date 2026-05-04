# Duplicate-PO Metadata Contract (ADR-028 Guard-rail 1)

**Status:** V1 enforcement — orchestration-tail (`build_analysis`).
**Write-time DB-level enforcement:** deferred to V1.5 (see §V1.5 below).
**Source:** ADR-028 §"Guard-rail 1: Documented metadata contract".
**Owners:** Backend (write path) + Compliance (audit-bearing field
classification per `compliance/audit_bearing_registry.yaml`).

---

## Why this exists

ADR-028 adopts the unified ASOE exception lifecycle for V1 with four
guard-rails. **Guard-rail 1** addresses E1's (DDD) concern that without
an explicit metadata schema, `OrderEvent.metadata` and
`ExecutionLog.outputs` (recipe_output) become a JSONB junk-drawer
within two intents.

This document **is** that explicit schema for `DUPLICATE_PO`. Adding
keys not listed here, or changing the types of listed keys, is a
contract change that requires updating this document **and** the
audit-bearing registry rows in the same PR.

---

## OrderEvent.metadata — input contract

For events that route to `DuplicatePORecipe.py`
(`event.event_type` matches `*DUPLICATE*`), `OrderEvent.metadata`
**MAY** contain the following keys:

| Key | Type | Required | Source | Description |
|---|---|---|---|---|
| `signal_scores` | `Dict[str, float]` | yes | upstream classifier | Per-signal match scores in `[0.0, 1.0]`. Expected keys: `po_number, customer_id, line_items, amount, timestamp, ship_to, channel, delivery_date`. Missing keys default to `0.0` (conservative). |
| `matched_po_id` | `str` | yes | upstream classifier | Identifier of the existing PO this event is being scored against. Surfaces in the canonical envelope (ADR-028 G2). |
| `tenant_id` | `str` | optional in V1 | upstream classifier | Tenant identifier. V1 file-backed resolver does not consume this; A9 will extend `gateways/tenant_config.py` to use it once the `tenant_config` table lands. |
| `customer_tier` | `Literal["strategic", "standard", "smb"]` | optional | admin tagging | Customer tier for L3 overrides. V1 file-backed resolver carries no tier-level weight overrides. |
| `channel` | `str` | optional | upstream classifier / EDI envelope | Source channel identifier (e.g. `"EDI"`, `"PORTAL"`). V1 file-backed resolver carries no channel-level weight overrides. |
| `behavior_tag` | `Literal["blanket_po", "drop_ship", "high_frequency"]` | optional | admin tagging | Behavior tag that selects a preset L4 partial weight override per `customer_behavior_overrides` in `config-defaults.json`. |

**Forbidden keys (V1):**

- Any key not listed above whose value is consumed by the recipe or its
  dependencies. Free-form `metadata` keys outside this list are tolerated
  for cross-cutting concerns (tracing, debugging, propagation) but
  **MUST NOT** influence the recipe's scoring or routing.

The Pydantic submodel `contracts.duplicate_po_contract.DuplicatePOEventMetadata`
encodes these rules. Cross-cutting keys flow through opaquely
(`extra="allow"`); declared keys are typed; `signal_scores` keys + range
are checked outside the model so per-key diagnostics surface in the
audit-trail explanation.

---

## ExecutionLog.outputs (recipe_output) — output contract

For executions of `DuplicatePORecipe.py`, `ExecutionLog.outputs`
**MUST** contain the following keys (every key produced by the recipe
is listed here; per ADR-028 G1, the recipe is the source of truth and
the contract simply mirrors its declared output shape):

| Key | Type | Description |
|---|---|---|
| `status` | `Literal["BLOCKED", "REVIEW_REQUIRED", "SOFT_FLAG", "PASS"]` | Recipe-level status. Mapped to `TerminalStatus` by `execute_recipe`. |
| `composite_score` | `float` | Weighted aggregate of `signal_scores`, rounded to 6 decimals. |
| `classification` | `Literal["AUTO_BLOCK", "REVIEW_REQUIRED", "SOFT_FLAG", "PASS"]` | Decision-tier label; mirrors `status` for non-`BLOCKED` cases. |
| `recommended_action` | `AllowedResolutionAction` | One of `BLOCK_AND_NOTIFY \| MERGE \| SUPERSEDE \| ALLOW_BOTH \| ESCALATE \| REQUEST_BUYER_CONFIRMATION` (see `constraints/specs.py`). |
| `autonomy_level` | `Optional[Literal["L1", "L2", "L3", "L4"]]` | Resolved from `DUPLICATE_PO_AUTONOMY_LEVELS` for the chosen action. `None` when no autonomy mapping is provided (e.g. tests). |
| `notification_template` | `Optional[str]` | One of `duplicate_po_blocked \| duplicate_po_amended \| duplicate_po_inquiry \| None`. Fed into the `buyer_notification` gateway effect. |
| `signal_breakdown` | `Dict[str, float]` | Per-signal weighted contribution; keys equal `_WEIGHTS.keys()`. The audit envelope (ADR-028 G2) reconstructs the score from this map. |
| `incoming_po_number` | `str` | Echo of the input — surfaces on the canonical envelope. |
| `customer_id` | `str` | Echo of the input — surfaces on the canonical envelope. |

**Forbidden keys:** any key not listed above. The recipe MUST NOT emit
unstructured payloads on `outputs` for this intent.

The Pydantic submodel `contracts.duplicate_po_contract.DuplicatePORecipeOutput`
encodes these rules with `extra="forbid"` — the recipe surface is fixed,
any drift is a contract bug regardless of intent.

---

## Audit-bearing classification

Per the Verdict 2026-04-22 model, the following keys are
**audit-bearing** for `DUPLICATE_PO` and MUST be populated for the
record to clear `build_analysis`:

- `signal_breakdown` — operator needs to see why the score was what it was
- `composite_score` — single-number summary the analyst signs off on
- `recommended_action` — the proposed disposition
- `classification` — the tier label
- `autonomy_level` — explains why the action did or did not auto-execute
- `incoming_po_number`, `customer_id` — record identity

Their precise rows in `compliance/audit_bearing_registry.yaml` are
maintained alongside this document; see the registry's
`DuplicatePOAnalysisData` class block (when it lands as part of the
canonical envelope work — A6).

The `tenant_config.contribution_trace` value persisted into
`enrichment_context["tenant_config"]` is also audit-bearing once the
canonical envelope endpoint (A6) consumes it for the per-layer
config-trace section.

---

## V1 enforcement (live — `orchestration/nodes.py::build_analysis`)

`build_analysis` runs a contract-coverage check before the existing
registry-coverage check, scoped to records whose `state.intent ==
DUPLICATE_PO`. Two validations:

1. **Input contract:** `state.event.metadata` validated via
   `validate_duplicate_po_event_metadata`.
2. **Output contract:** `state.execution_log.outputs` validated via
   `validate_duplicate_po_recipe_output`, but only when the recipe
   actually executed (execution_log present + outputs populated +
   `selected_recipe == "DuplicatePORecipe.py"`). Earlier halt paths
   — BLOCKED by shadow, no_recipe, etc. — leave `execution_log` empty;
   those records are legitimately absent the recipe-output dict.

On violation: route to `TerminalStatus.AUDIT_CONTEXT_MISSING` with an
explanation that names the offending key(s) and the contract name.
Same routing the registry-coverage check uses, so auditors see
"compliance data was malformed" rather than a Pydantic ValidationError
stack trace. The contract gate runs **before** the registry-coverage
check so a contract violation surfaces as the precise root cause
rather than as a downstream missing-field symptom.

Tests: `tests/test_metadata_contract.py` (unit + integration paths
including build_analysis routing).

### Limits of V1 enforcement

- Records that take a path NOT going through `build_analysis`
  (currently none — `build_analysis` is the tail of every graph
  invocation) are not validated. If a future code path bypasses
  `build_analysis`, V1 enforcement is silent on those writes.
- Direct callers of `db/repository.py::ExceptionRepository.create()`
  outside the standard graph flow (admin tooling, data migrations,
  re-import scripts) are not validated. V1.5 closes this gap.

---

## V1.5 enforcement (deferred — `db/repository.py` write-time check)

The follow-up implementation will add a write-time check inside
`ExceptionRepository.create()` and `update()` that re-runs the same
validators as the V1 orchestration gate. Goals:

1. **Last line of defense.** Any path that writes to the exception
   store, including non-graph callers, sees the contract enforced.
2. **No duplicated logic.** The DB-level check imports the SAME
   validators from `contracts.duplicate_po_contract` — single source
   of truth (compare WeightContractViolation pattern in
   `recipes/DuplicatePORecipe.py`).
3. **Failure routing.** On violation: raise
   `MetadataContractViolation` (the existing exception type). Callers
   are responsible for catching and routing to AUDIT_CONTEXT_MISSING
   the same way the V1 gate does. The DB layer never silently
   downgrades an audit failure to a generic 500.
4. **Observability.** Surface a `metadata_contract_violation` metric
   on the existing observability pipeline so contract drift is visible
   without waiting for an audit.
5. **Tests.**
   - `test_repository_metadata_contract_v15.py` — write-time rejection
     paths for all three contract failure modes.
   - Audit-registry coverage matches this document (verified by the
     existing `tests/test_audit_registry_coverage.py`).

A backstop CI grep / static check is not needed — the DB-layer check
itself is the backstop.

---

## How to evolve this contract

1. Open a PR that updates this document **and** the matching Pydantic
   submodel in `contracts/duplicate_po_contract.py` in the same PR.
2. Update any matching `compliance/audit_bearing_registry.yaml` rows
   in the same PR.
3. The compliance team is CODEOWNERS of the registry; they review
   the audit-bearing classification.
4. Run vocabulary-sync tests in `tests/test_constraints.py`,
   metadata-contract tests in `tests/test_metadata_contract.py`, and
   (post V1.5) `tests/test_repository_metadata_contract_v15.py`.
5. Update `recipes/DuplicatePORecipe.py` if the recipe surface changes.
6. Update `recipes/registry.py::expected_metadata_keys` for
   DuplicatePORecipe to reflect any added/removed event-side keys.

---

## References

- `docs/adr/ADR-028-duplicate-po-storage-shape.md` — Guard-rail 1
- `docs/specs/duplicate-po/2026-05-03-design-review.md` — Item 1 / D1 + Item 2
- `docs/specs/duplicate-po/2026-05-04-step0-bucketed-mapping.md` — §7
  metadata contract row
- `contracts/duplicate_po_contract.py` — Pydantic submodels + validators
- `orchestration/nodes.py::build_analysis` — V1 enforcement point
- `recipes/DuplicatePORecipe.py` — recipe surface
- `recipes/registry.py` — `expected_metadata_keys` declaration
- `compliance/audit_bearing_registry.yaml` — audit-bearing field
  classifications (rows for `DuplicatePOAnalysisData` land alongside A6)
