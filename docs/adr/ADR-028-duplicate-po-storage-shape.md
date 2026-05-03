# ADR-028: Duplicate-PO Storage Shape — Unified Exception Lifecycle with Four Guard-Rails

**Status:** Accepted
**Date:** 2026-05-03
**Deciders:** Architecture review chair; backend tech lead; data architect; SOX/compliance lead; ML/feature-store lead; B2B/ERP integration lead; CS associate, CS manager, tenant admin (end-user representation). Multi-perspective review session 2026-05-03 (`docs/specs/duplicate-po/2026-05-03-design-review.md`).
**Applies to:** `db/migrations/`, `db/repository.py`, `api/analysis_composer.py`, `api/routes/exceptions.py`, `contracts/models.py`, `recipes/DuplicatePORecipe.py`, `recipes/registry.py`, `tests/test_repository.py`, `tests/test_audit_chain.py`.
**Supersedes / relates to:** ADR-022 (database access pattern), ADR-023 (disposition + hash-chained audit), ADR-024 (OM coverage expansion).

---

## Context

The new `b2b-duplicate-po-check` skill specification (full text under `docs/specs/duplicate-po-product-spec.md`, reference files under `docs/specs/duplicate-po/`) prescribes four dedicated PostgreSQL tables for the duplicate-PO domain:

```
incoming_po              -- staging row per inbound PO header
incoming_po_lines        -- staging line items
duplicate_check_results  -- one row per detection: composite_score, signal_breakdown,
                            classification, recommended_action, autonomy_level,
                            resolution fields
duplicate_check_audit    -- per-action immutable audit log
```

ASOE today has a deliberately **unified** exception lifecycle that already serves nine intents (`CONTRACTUAL_CORRECTION`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO`, `PRICE_HOLD_RELEASE`, `EDI_MISMATCH`, `BACK_ORDER`, `OVER_MAX`, `MIN_ORDER_QTY`, `PALLET_CONFIG`, `DELIVERY_DELAY`):

```
OrderEvent           -- canonical inbound event with metadata JSONB escape hatch
Exception            -- unified per-event lifecycle row
ExecutionLog         -- per-recipe-execution record (recipe_output JSONB, signal_breakdown lives here)
audit_hash_chain     -- ADR-023 immutable hash-chained audit
```

The spec assumes per-intent tables. ASOE assumes one shape across intents. Without an explicit decision, every new intent re-opens the question, JSONB drifts into a junk drawer, and read paths fragment.

Multi-perspective review (DDD, data-intensive systems, CQRS, multi-tenant SaaS, SOX, ML/feature-store, ERP veteran, plus end-user representation) converged on a single answer with conditions. This ADR captures it.

---

## Decision

**Adopt the unified ASOE exception lifecycle as the V1 storage shape for `DUPLICATE_PO`.** No new per-intent tables. Spec entities map onto existing storage as follows:

| Spec entity (`docs/specs/duplicate-po/schema.sql`) | ASOE storage |
|---|---|
| `incoming_po` (header) | `OrderEvent` row + `OrderEvent.metadata` JSONB |
| `incoming_po_lines` | `OrderEvent.metadata.lines: List[Line]` |
| `incoming_po.raw_payload` | `OrderEvent.metadata.raw_payload` JSONB |
| `incoming_po.po_number_norm` | `OrderEvent.metadata.po_number_norm` (set by upstream normalizer) |
| `duplicate_check_results.composite_score`, `signal_breakdown`, `classification`, `recommended_action`, `agent_reasoning`, `autonomy_level` | `ExecutionLog.recipe_output` JSONB |
| `duplicate_check_results.matched_po_id` | `ExecutionLog.recipe_output.matched_po_id` |
| `duplicate_check_results.resolved_by`, `resolved_action`, `resolved_at`, `resolution_notes` | `ExecutionLog.resolved_by`, `resolved_action`, `resolved_at`, `resolution_notes` (already first-class) |
| `duplicate_check_audit` | `audit_hash_chain` (ADR-023) |
| `incoming_po.status` (`PENDING|PROCESSING|PASSED|FLAGGED|BLOCKED|RESOLVED`) | Existing `Exception.lifecycle` enum (mapped) |
| `incoming_po.tenant_id` (RLS) | `OrderEvent.tenant_id` + repository-level filtering |

The decision is binding for V1 and revisited only on the explicit triggers in **ADR-031** (read-projection split trigger).

### Four binding guard-rails

The unified shape is defensible only if the following four guard-rails ship with V1. Each is itself enforceable, not aspirational.

#### Guard-rail 1 — Documented metadata contract (DDD anti-junk-drawer)

The keys allowed in `OrderEvent.metadata` and `ExecutionLog.recipe_output` for `DUPLICATE_PO` are explicitly enumerated and validated at write-time.

For `OrderEvent.metadata` when `event_type` matches `DUPLICATE_*`:

| Key | Type | Required | Source |
|---|---|---|---|
| `po_number_norm` | str | yes | upstream normalizer |
| `lines` | list[{sku, qty, unit_price, description?}] | yes | upstream parser |
| `signal_scores` | dict[str, float in [0,1]] keyed by the 8 signals | yes | upstream candidate-retrieval + scoring layer |
| `matched_po_id` | str | yes | upstream candidate-retrieval |
| `raw_payload` | object (EDI/JSON/XML as received) | yes | ingestion connector |
| `submission_channel` | enum: `EDI`/`API`/`EMAIL`/`PORTAL` | yes | ingestion connector |
| `submitted_at` | RFC3339 timestamp | yes | ingestion connector |
| `total_amount` | decimal | optional | upstream parser |
| `currency` | ISO-4217 | optional | upstream parser |
| `ship_to_address` | object | optional | upstream parser |
| `requested_delivery` | ISO-8601 date | optional | upstream parser |

For `ExecutionLog.recipe_output` produced by `DuplicatePORecipe.py`:

| Key | Type | Required | Producer |
|---|---|---|---|
| `status` | `BLOCKED`/`REVIEW_REQUIRED`/`SOFT_FLAG`/`PASS` | yes | recipe |
| `composite_score` | float | yes | recipe |
| `classification` | `AUTO_BLOCK`/`REVIEW_REQUIRED`/`SOFT_FLAG`/`PASS` | yes | recipe |
| `recommended_action` | `AllowedResolutionAction` literal | yes | recipe |
| `autonomy_level` | `L1`/`L2`/`L3`/`L4` | optional | recipe (when policy mapping injected) |
| `notification_template` | str or null | yes | recipe |
| `signal_breakdown` | dict[str, float] (weighted contribution per signal) | yes | recipe |
| `incoming_po_number` | str | yes | recipe (echo) |
| `customer_id` | str | yes | recipe (echo) |

**Enforcement mechanism:** new `contracts/metadata_schemas.py` module exposes a `validate_metadata(intent: Intent, metadata: dict) -> None` and `validate_recipe_output(recipe_name: str, output: dict) -> None`. Both are called from `db/repository.py` write paths and from `recipes/executor.py` post-execution. Failure raises `MetadataContractViolation` and routes the event to `FAIL_TO_HUMAN` with the violation captured in the audit chain. Contracts are versioned; the version travels in metadata.

#### Guard-rail 2 — Canonical read API

A single endpoint returns the entire reconstructable duplicate-PO record in one round trip, eliminating UI-side stitching and giving SOX a single citation point.

```
GET /api/v1/exceptions/duplicates/:id
```

Response envelope:

```json
{
  "exception_id": "...",
  "tenant_id": "...",
  "incoming_po": { ... },           // projected from OrderEvent + metadata
  "matched_po": { ... } | null,     // projected from gateway-resolved data
  "detection": {
    "composite_score": 0.847,
    "classification": "REVIEW_REQUIRED",
    "recommended_action": "BLOCK_AND_NOTIFY",
    "autonomy_level": "L2",
    "agent_reasoning": "...",
    "signal_breakdown": { ... }
  },
  "human_actions": [ ... ],         // resolved_by/action/notes if present
  "audit_trail": [ ... ]            // ordered audit_hash_chain entries
}
```

Implemented as a composer in `api/analysis_composer.py` (alongside the existing per-intent composers). Single SQL round trip preferred; two if `audit_hash_chain` is too expensive in the same query (latency budget per Guard-rail 4 below).

#### Guard-rail 3 — Pre-committed read-projection split trigger

The conditions under which we will revisit this decision and split out a per-intent read projection (`duplicate_check_results` materialized view, escalated to physical table only if MV is insufficient) are written down once, in **ADR-031**, and not relitigated outside those triggers. This protects the unified shape from death-by-a-thousand-questions and gives ML / data leads a defined exit ramp.

#### Guard-rail 4 — Hash-chain coverage proof + tenant-isolation CI gate

Two CI-enforced checks land alongside V1:

1. **Hash-chain coverage test** (`tests/test_audit_chain.py::test_jsonb_mutation_chained`): for each write path that mutates `OrderEvent.metadata` or `ExecutionLog.recipe_output`, a corresponding entry must appear in `audit_hash_chain` within the same transaction. Test harness uses a `MutatingRepositoryProxy` that records every mutation and asserts a chain entry exists with the matching content hash. SOX requirement, non-negotiable.
2. **Tenant-isolation lint** (`tests/test_repository.py::test_all_queries_filter_tenant`): static walk over `db/repository.py` AST asserts every `SELECT`/`UPDATE`/`DELETE` either includes a `tenant_id` predicate or is annotated `# pragma: cross-tenant <reason>`. Build fails on missing pragma.

Latency budget for the read API (`GET /api/v1/exceptions/duplicates/:id`): **P95 ≤ 400 ms** at V1 volume; breach triggers ADR-031 evaluation.

---

## Rationale

Convergent reasoning from the seven expert lenses (full transcript: `docs/specs/duplicate-po/2026-05-03-design-review.md`):

- **CQRS / event-sourcing:** `OrderEvent` → `ExecutionLog` → `audit_hash_chain` is already an event-sourced write side. A parallel write model creates two places where state can drift. Per-intent tables, if needed, belong as read projections.
- **DDD / bounded context:** The unified shape is honest only if `Exception` has invariants writable without "depends on intent." Guard-rail 1 (explicit metadata contract per intent) closes that gap.
- **Data-intensive systems (Kleppmann lens):** For V1 query patterns (queue listing, single-detail lookup), unified + a couple of GIN indexes on JSONB keys is adequate. The split trigger (ADR-031) handles the future.
- **SOX / compliance:** Audit trail completeness and a single canonical read are the only things that matter. Guard-rails 2 and 4 deliver both.
- **Multi-tenant SaaS:** Tenant isolation matters more than table layout. Guard-rail 4's CI gate enforces it.
- **ML / feature store:** Will accept unified for V1 *only with* the pre-committed split trigger that proactively fires when calibration is scheduled (ADR-031).
- **B2B / ERP integration veteran:** Both shapes ship in production. Spend the saved effort on amendment-PO and cross-subsidiary modeling, not on schema relitigation.

End-user representation:
- **CS associate (U1):** Layer-2 detail must load in one fetch. Guard-rail 2 satisfies.
- **CS manager (U2):** Bulk export of resolved exceptions for QBR. Backlog item, not blocker.
- **Tenant admin (U3):** Cares about config (separate ADR-030); indifferent on storage shape.

---

## Phased rollout

### V1 (this ADR's scope)

1. `contracts/metadata_schemas.py` — define `DUPLICATE_PO` contracts; expose validators.
2. `db/repository.py` — invoke `validate_metadata` and `validate_recipe_output` on every write touching `OrderEvent.metadata` or `ExecutionLog.recipe_output`.
3. `api/analysis_composer.py` — add `compose_duplicate_po_envelope(exception_id) -> DuplicatePOEnvelope`.
4. `api/routes/exceptions.py` — add `GET /api/v1/exceptions/duplicates/:id` returning the envelope.
5. `api/schemas.py` — add `DuplicatePOEnvelope`, `IncomingPOProjection`, `MatchedPOProjection`, `DetectionProjection`, `HumanActionEntry`.
6. `tests/test_audit_chain.py::test_jsonb_mutation_chained` — coverage proof.
7. `tests/test_repository.py::test_all_queries_filter_tenant` — tenant-isolation lint.
8. `tests/test_metadata_schemas.py` — contract validation positive/negative cases for `DUPLICATE_PO`.

### V1.5 (follow-up)

- Bulk export endpoint `GET /api/v1/exceptions/duplicates/export?from=…&to=…&format=csv|jsonl` (CS-manager request).
- Apply Guard-rail 1 contract enforcement retroactively to the other 8 intents (each gets its own contract block in `metadata_schemas.py`).

### V2 (conditional on ADR-031 trigger firing)

- Materialized view `mv_duplicate_check_results` projecting from `OrderEvent` + `ExecutionLog` for fast analytics and (future) calibration training data.

---

## Consequences

### Positive

- One audit boundary, one source of truth for state.
- Adding a new intent does not require a schema migration — only a new contract block in `metadata_schemas.py`, a recipe, and vocabulary entries.
- The canonical read API serves both UI Layer-2 deep-dive and SOX audit-trail evidence with one implementation.
- Hash-chain coverage proof closes the "JSONB silently mutated without audit" risk that would otherwise be load-bearing on reviewer trust.

### Negative

- Querying duplicate-PO-specific shapes goes through JSONB operators (`metadata->>'matched_po_id'`, `recipe_output->>'composite_score'`). Acceptable at V1 volume; ADR-031 trigger handles the future.
- Metadata-contract enforcement adds a write-path validator. Cost is microseconds; benefit is preventing silent schema drift.
- The "what does `Exception` actually mean" DDD critique is partially mitigated, not eliminated. Future intents that genuinely don't fit this aggregate (e.g., a multi-document workflow spanning several events) will need their own ADR.

### Compliance notes

- Every `DUPLICATE_PO` field a SOX auditor cares about (composite score, signal breakdown, recommended action, actual resolution, actor identity, timestamps) is reachable from `GET /api/v1/exceptions/duplicates/:id` and reconstructable from `audit_hash_chain` alone.
- Metadata-contract version travels in `OrderEvent.metadata.contract_version`. Auditor reproducing a record uses the matching contract version to interpret the JSONB.
- Tenant isolation is now CI-enforced; no production query can ship that bypasses `tenant_id` filtering without an explicit annotated pragma.

---

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Per-intent tables (verbatim spec)** | Creates a parallel write model alongside ASOE's event-sourced one. Two sources of truth for resolution state means inevitable drift; doubles audit-chain integration cost. CQRS, ERP-veteran, and multi-tenant lenses all dispreferred. |
| **Unified write + immediate per-intent projections (V1)** | Premature optimization at V1 query volume. ML/feature-store lens accepted V1 unified-only conditional on the pre-committed split trigger (ADR-031). |
| **Unified, no metadata contract** | DDD lens explicit objection: JSONB becomes a junk drawer within two intents. Contract is cheap; absence is expensive. |
| **Per-intent enum-typed columns on `Exception`** | Locks the schema to today's intents; every new intent is a migration. Defeats the loose coupling that makes the unified lifecycle valuable. |

---

## Open questions

- Performance budget for the read envelope when `audit_hash_chain` is large (>10k entries per exception). Likely needs a `?audit_limit=N` parameter; deferred to first integration test that breaches budget.
- Whether `MatchedPOProjection` should embed the matched PO's full lines or just a reference. Current decision: full lines (UI Layer-2 needs them); revisit if envelope size becomes a problem.
- Cross-tenant aggregate analytics (a question ML will surface eventually) — out of scope for this ADR; will need its own ADR with explicit cross-tenant authorization model.

---

## References

- `docs/specs/duplicate-po-product-spec.md` — full spec
- `docs/specs/duplicate-po/schema.sql` — original spec schema (preserved as reference, not implemented)
- `docs/specs/duplicate-po/2026-05-03-design-review.md` — meeting transcript
- ADR-022, ADR-023, ADR-024, ADR-025
- ADR-029 (override merge policy), ADR-030 (config hierarchy), ADR-031 (split trigger), ADR-032 (calibration deferral), ADR-033 (reason-code vocabulary)
