# Audit-Bearing Field Registry — Workshop Minutes

**Convened:** 2026-04-22
**Duration:** 90 min
**Attendees:** Dana (Compliance — veto), Priya (Product), Ben (Backend),
Ulla (UI)
**Chair:** Platform
**Artifact produced:** `compliance/audit_bearing_registry.yaml`

---

## Purpose

Classify every field on every `*AnalysisData` Pydantic model
(consumed by the UI's exception detail surface) as one of:

- **audit-bearing** — must be populated at composition time; absence
  routes the exception to `AUDIT_CONTEXT_MISSING` (new structured
  terminal status, distinct from `FAIL_TO_HUMAN`).
- **conditional** — audit-bearing only when a named predicate holds
  (typically `resolved_action == X`). Contextual otherwise.
- **contextual** — Layer 2 evidence. UI structurally omits when
  absent; never rendered as "—".

## Three-question classification test

Applied per field; first YES wins.

1. **Q1 — Does the Compliance Shadow reference this field in a
   `policy_hits` value?** Mechanical extract from
   `constraints/fallback_backend.py`. Auto-audit-bearing.
2. **Q2 — Does any SOX control narrative, four-eyes threshold rule,
   or override policy name this field?** Requires compliance sign-off.
3. **Q3 — Did the recipe consume this field as a deterministic-branch
   input?** Auto-audit-bearing.

Anything failing Q1/Q2/Q3 defaults to **contextual**.

## Pre-read — Backend Ben's mechanical extract

All shadow policy hits in `constraints/fallback_backend.py` keyed by
field (auto-audit-bearing per Q1):

| Recipe domain | Fields | Hit vocabulary |
|---|---|---|
| PriceHoldRelease | `po_price`, `sap_base_price`, derived `variance_pct` | `PRICE_HOLD_*` family |
| EdiMismatch | `mismatch_sub_type` | `EDI_*_MISMATCH_*`, `EDI_SHIP_TO_ESCALATE` |
| BackOrder | `ordered_qty`, `available_qty`, derived `gap_pct` | `BACK_ORDER_*` family |
| OverMax | `total_ordered`, `max_qty`, derived `exceedance_pct` | `OVER_MAX_*` family |
| MOQ | `ordered_qty`, `moq_qty`, derived `shortfall_pct` | `MOQ_*` family |
| PalletConfig | per-line `fill_pct` | `PALLET_CONFIG_*` family |
| DeliveryDelay | `planned_date`, `projected_eta`, derived `days_late` | `DELIVERY_DELAY_*` family |
| DuplicatePO | per-signal scores, derived `composite_score` | `DUPLICATE_PO_*` family |
| Cross-cutting | `batch_total_variance` | `CIRCUIT_BREAKER_VARIANCE`, `MASS_UPDATE_DETECTED`, `HITL_REQUIRED_FOR_SYSTEMIC_FAILURE` |

Four-eyes gate in `contracts/policy.py:102-107` (`POLICY_FOUR_EYES_THRESHOLD`):
any field feeding `financial_impact_usd` is audit-bearing (§404).

## Cross-cutting issues surfaced

1. **Conditional audit-bearing tier required.** Registry schema needs
   `tier: conditional` with a `depends_on:` predicate. ~8 fields
   (BackOrder `alternate_warehouses` / `substitutes` / `production` /
   `inbound_po`, DeliveryDelay `alternate_options`, etc.) — binary
   classification would force false all-required or all-optional.

2. **Gateway dependency promotions.** 11 audit-bearing fields are
   gateway-sourced. The gateway SLA becomes a compliance constraint:
   gateway failure fails the exception. Correct posture; operational
   commitment.

3. **Pre-existing gap in PriceAnalysis.** Six fields
   (`doc_type`, `doc_number`, `rule_id`, `root_cause_category`,
   `contract_ref`, `promotion_ref`) classified audit-bearing but no
   current gateway/recipe populates them. Without a grandfather
   clause, every CONTRACTUAL_CORRECTION exception would fail after
   the node ships. **Dana approves a 60-day clause with deadline
   2026-06-21**, archived in the YAML under `grandfather_clauses`.

4. **`action` / `recommended_action` / `classification` are always
   audit-bearing.** Hard-coded in the YAML's `conventions` block so
   adding a new recipe inherits the rule.

5. **Derived fields** (e.g., `variance_pct`, `gap_pct`,
   `exceedance_pct`) carry a `derivation:` annotation so auditors
   can reconstruct them from raw inputs.

## Final tallies

| Tier | Count | %  |
|---|---:|---:|
| audit-bearing | 64 | 67% |
| conditional | 8 | 8% |
| contextual | 24 | 25% |
| **Total** | **96** | — |

- 8 audit-bearing fields are `status: backend-gap` (no current producer).
- 11 audit-bearing fields are `gateway_dependency: true`.

## Sign-offs

**Dana (Compliance — veto holder):** conditional on three requirements,
all satisfied by the YAML artifact:
1. YAML under `asoe2/compliance/` with compliance-team CODEOWNERS
   rule (TODO: add path rule).
2. `build_analysis` node emits `AUDIT_CONTEXT_MISSING` terminal
   distinct from `FAIL_TO_HUMAN`, visible in trace.
3. PriceAnalysis grandfather clause with 60-day deadline, documented.

**Priya (Product):** signed. Conditional tier preserves UI fidelity
while eliminating partial-truth states.

**Ben (Backend):** signed. Sequencing:
(a) composition node + easy adapters first (PHR/EDI + MOQ, OverMax,
    Pallet, DeliveryDelay — ~2 weeks).
(b) gateway-persistence work for BackOrder/Duplicate (~1 sprint).
(c) PriceAnalysis gateway work before the 2026-06-21 deadline.

**Ulla (UI):** signed. Will write `useConditionalField(field, resolvedAction)`
hook + per-section visibility refactor (~half a sprint).

## Owed deliverables

1. ✅ `compliance/audit_bearing_registry.yaml` (this commit).
2. ✅ `compliance/audit_bearing_registry.md` (this file).
3. ⏳ New terminal `AUDIT_CONTEXT_MISSING` in
   `contracts/models.py::TerminalStatus`.
4. ⏳ `api/analysis_composer.py` (the `build_analysis` node).
5. ⏳ CI test iterating every `*AnalysisData` Pydantic class asserts
   every field appears in the registry.
6. ⏳ CODEOWNERS path rule routing `compliance/**` to the compliance
   team.
7. ⏳ `useConditionalField()` UI hook + per-section refactor.
8. ⏳ Grandfather clause tracking issue before the 2026-06-21 deadline.
