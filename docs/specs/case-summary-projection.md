# Case Summary Projection — Phase 0 Tickets

**Parent ADR:** ADR-041 P3e (`docs/adr/ADR-041-p3e-cases-row-and-analysis-reorder.md`).
**Owner:** asoe2 backend.
**Blocks:** `NEXT_PUBLIC_CASES_ROW_V2` flag flip in asoe-ui.

## Phase 0a status (2026-05-28)

T1 + T2-minimal + T3 + T4 shipped on branch
`claude/brave-curie-SHt83`. Scope of the **deferred T2 per-intent
template work** documented at the end of this file under "Phase 0b
follow-on (Recipe team)".

T1, T3, T4 are complete. T2 ships the always-available projection
fields (verdict color rollup, intent, customer_name, dollar_impact).
The per-intent `problem_one_liner` and `top_line_sku_*` fields ship
as `null` pending the Recipe team's template implementations — see
"Phase 0b follow-on" below.

This document is the ticket scope for Phase 0 of ADR-041 P3e. Four
tickets, sequenceable in the order listed.

---

## Ticket 1 — `CaseListItem` schema extension

**File:** `api/schemas.py`
**Effort:** ~½ day
**Tests:** unit (schema validation) + lock (mock-data mirror)

### Scope

Extend `CaseListItem` (the response shape of
`GET /api/v1/cases`) with seven new optional fields. **Do not modify
`contracts/models.py::OrderCase`** — these are read-projection fields,
not persisted columns (Guardrail #3 in asoe-ui CLAUDE.md).

```python
class DollarImpact(BaseModel):
    amount_cents: int
    currency: str  # ISO 4217

class CaseListItem(BaseModel):
    # ... existing fields ...
    customer_name: Optional[str] = None
    top_line_sku_code: Optional[str] = None
    top_line_sku_title: Optional[str] = None
    problem_one_liner: Optional[str] = None
    intent: Optional[str] = None        # runtime enum, not Literal
    dollar_impact: Optional[DollarImpact] = None
    audit_verdict_color: Optional[Literal["R", "A", "G"]] = None
```

### Acceptance

1. All seven fields nullable; nothing on the existing response shape
   becomes required.
2. `intent` is `Optional[str]`, not a `Literal` — Guardrail #1
   (no hardcoded enum vocabulary; intent is runtime-fetched).
3. `audit_verdict_color` uses `Literal["R", "A", "G"]` — these are
   shadow-rollup constants, not extensible.
4. Currency is ISO 4217 string (Compliance partial-truth concern —
   bare amount without currency is a Guardrail #6 violation).
5. Architectural lock: `tests/test_schemas.py::test_case_list_item_new_fields_nullable`.
6. Mirror in asoe-ui at `src/lib/api.ts::CaseListItem` (UI ticket).

### Out of scope

* Populating the fields — see Tickets 2-4.
* RBAC filtering — see Ticket 4.

---

## Ticket 2 — Rollup writer + per-intent one-liner composition

**Files:**
* `api/case_resolver.py` (rollup orchestration)
* `api/analysis_composer.py` (per-intent template dispatch)
* `recipes/*/recipe.py` (per-intent template strings)
**Effort:** ~3-4 days (recipe touch points dominate)
**Tests:** unit per intent + architectural lock on composer dispatch

### Scope — `build_case_summary` graph node

A new orchestration node mounted after `build_analysis`. Reads
`state.enrichment_context` + `state.child_records` and projects:

* `customer_name` ← customer-service lookup keyed on `OrderCase.customer_id`
* `top_line_sku_code` + `top_line_sku_title` ← child record with
  `max(line_value)` per Recipe SME §4 rule. For OVERMAX/MOQ/PALLET
  drill into `trim_plan` / `round_up_plan` head entry.
* `problem_one_liner` ← per-intent template (next section).
* `intent` ← `child.intent` (first child for single-record cases;
  primary child for multi-record per the same `max(line_value)` rule).
* `dollar_impact` ← per-intent rule (next section).
* `audit_verdict_color` ← shadow rollup (Ticket 3).

The projection writes to a new `CaseSummary` row (table
`case_summary`, FK to `order_case.case_id`) regenerated on every
`build_analysis` invocation. The list endpoint joins the row.

**Why a side table, not columns on `order_case`:** the rollup is
read-projection; the source of truth is `enrichment_context` +
shadow output. Side table makes regeneration on reanalysis a single
`UPSERT` without touching the parent row's `updated_at`. Aligns
with ADR-031 (read-projection split trigger).

### Per-intent one-liner templates

Verbatim from the Recipe SME panel response. Implemented as a
registry keyed on `intent`, dispatched by `analysis_composer.py`.

| Intent | Template | Fields |
|---|---|---|
| PRICE_DISCREPANCY | `{sku} — {material_desc} (PO ${po_unit_price}/{uom} vs ERP ${erp_unit_price}; {variance_pct}% delta, ${total_at_risk} at risk)` | `price_analysis.*` |
| DUPLICATE_PO | `PO {duplicate_order.po_number} — duplicate of {original_order.po_number} ({days_between}d apart, {confidence}% match)` | `duplicate_detection.*` |
| ORDER_COMPARISON / BACKORDER | `{sku} — short stock: {available_qty}/{ordered_qty} {uom} ({gap_pct}% gap, ATP {atp_date})` | `backorder_analysis.*` + `price_analysis.sku` |
| OVERMAX | `{trim_plan[0].sku} — over max by {excess_qty} {uom} ({exceedance_pct}% over)` | `overmax_analysis.*` |
| MOQ | `{sku} — below MOQ by {shortfall_qty} {uom} (ordered vs {moq_qty} required)` | `moq_analysis.*` |
| PALLET | `{order_line_count} lines — {loose_cases_total} loose cases ({classification})` | `pallet_analysis.*` (no SKU) |
| DELIVERY_DELAY | `{affected_lines} line(s) — {days_late}d late ({delay_category}, ETA {projected_eta})` | `delivery_delay_analysis.*` |
| PRICE_HOLD | `{sku} — {hold_status}: PO ${po_price} vs SAP ${sap_base_price} ({variance_pct} variance, {action})` | `price_hold_analysis.*` + new `sku` field (see §Gaps) |
| EDI_MISMATCH | `{sub_type}: expected {expected_value}, received {received_value} ({classification})` | `edi_mismatch_analysis.*` |
| EMAIL_COMPLAINT / CHANGE_ANALYSIS | `{change_items[0].field}: {from_value} → {to_value} (rec: {decision.recommended_action}, ${revenue_impact_usd})` | `change_analysis.*` |
| EMAIL_COMPLAINT (pure intake) | **null** until recipe extends | gap — see §Gaps |

### Per-intent `dollar_impact` rule

| Intent | Source | Notes |
|---|---|---|
| PRICE_DISCREPANCY | `price_analysis.total_at_risk` | clean |
| BACKORDER | `backorder_analysis.at_risk` | clean |
| OVERMAX | `overmax_analysis.at_risk` | clean |
| MOQ | `moq_analysis.at_risk` | clean (uplift) |
| DELIVERY_DELAY | `delivery_delay_analysis.at_risk` | **null until gateway 2026-07-21**; do not zero-fill |
| CHANGE_ANALYSIS | `change_analysis.decision.revenue_impact_usd` | clean |
| DUPLICATE_PO | `duplicate_order.total_value` | **label: "Disputed amount"** (UI relabels per intent) |
| PRICE_HOLD | `(po_price − sap_base_price) × qty` | **null** until `qty` lands on `PriceHoldAnalysisData` |
| EDI_MISMATCH | — | **null** — sub-type drives meaning; no honest single number |
| PALLET | — | **null** — labor/freight waste, not revenue |
| EMAIL_COMPLAINT pure intake | — | **null** — no monetary field today |

### Audit verdict color gates (Recipe SME §5)

Never RED at intake (ceiling at AMBER):
* DELIVERY_DELAY when `days_late > 0` but no SLA breach
* PALLET BROKEN_LAYER / PARTIAL_PALLET
* EDI_MISMATCH sub-type ∈ {DATE_FORMAT, UOM_NORMALISATION}

Never GREEN (human eye required regardless of confidence):
* PRICE_HOLD with `action = AUTO_RELEASE` AND `variance_pct >
  tolerance_pct × 0.8`
* DUPLICATE_PO with `duplicate_order.total_value > $10k`
* Any case where `ImpactMetrics.sla_priority` is tier-1
* EMAIL_COMPLAINT / CHANGE_ANALYSIS with `decision.requires_cosign = true`

Implement as a `verdict_color_gate(intent, context, raw_color) -> color`
function applied at composer time. Architectural lock asserts all
ten gates above.

### Gaps requiring recipe extension

**PRICE_HOLD missing `sku` + `qty`:** Add `sku: Optional[str]` and
`qty: Optional[int]` to `PriceHoldAnalysisData`. Both fields are
populated by `PriceHoldReleaseRecipe`'s upstream context — surface
them.

**EMAIL_COMPLAINT pure intake has no quantity:** Add a
`complaint_analysis` block to the recipe output carrying
`received_qty`, `expected_qty`, `complaint_type`. Without this the
brief's example ("short shipment: received 380 of 480") cannot ship
honestly. Until landed: EMAIL_COMPLAINT intake `problem_one_liner`
stays null.

Either extend the recipe output (Verdict 2026-04-22 Pillar 1) OR
document under
`compliance/audit_bearing_registry.yaml::grandfather_clauses` with
compliance-approved deadline. No UI-side composition.

### Acceptance

1. Architectural test: every intent in the registry has either a
   template implementation OR a grandfather-clause entry.
2. Unit test per intent: template renders against canonical
   `enrichment_context` fixture.
3. Composer dispatches via registry, not `if intent == ...`
   branches (Guardrail #1 mirror).
4. `verdict_color_gate` unit-tested for all ten override rules.
5. `dollar_impact` null where the rule says null — never zero-fill.

---

## Ticket 3 — Transactional rollup `case_update` dispatch

**File:** `api/case_resolver.py`
**Effort:** ~1 day (mostly test writing)
**Tests:** architectural lock + integration

### Scope

Any code path mutating a child record's `lifecycle_state` such that
the rollup MAY flip `audit_verdict_color`, `problem_one_liner`, or
`dollar_impact` MUST emit a `case_update` event for the parent case
from the **same database transaction** as the child write.

This is the contract the asoe-ui WebSocket invalidation
(`useManualOrderCases.ts:170-178`) depends on. Today's
`case_update` dispatch happens, but is not enforced in-txn — a
crash between child commit and event emit leaks state divergence
into the operator's queue.

### Implementation sketch

```python
# api/case_resolver.py
async def apply_child_action(...):
    async with db.transaction() as tx:
        await tx.execute(child_update_stmt)
        # Regenerate case_summary in the same transaction.
        new_summary = await build_case_summary(case_id, tx=tx)
        await tx.execute(upsert_case_summary_stmt, new_summary)
        # Dispatch INSIDE the transaction — events flush on commit.
        await event_bus.dispatch(
            CaseUpdateEvent(case_id=case_id, summary=new_summary),
            tx=tx,
        )
```

### Acceptance

1. Architectural lock at `tests/architectural/test_rollup_dispatch_in_txn.py`
   — greps `api/case_resolver.py` for child-mutation call sites and
   asserts each is paired with a `case_update` dispatch on the same
   `tx` object.
2. Integration test: simulated mid-transaction failure (raise after
   child commit, before event dispatch) results in child state
   rolled back, not divergent.
3. Mirror lock in asoe-ui: `tests/architectural/case_pivot_mock_wiring.test.ts`
   asserts the mock layer's `casesApi.act()` returns a `case_update`
   event for every state-changing action.

---

## Ticket 4 — RBAC filter on `dollar_impact`

**File:** `api/routes_cases.py` (list endpoint)
**Effort:** ½ day
**Tests:** integration (per-role response shape)

### Scope

Strip `dollar_impact` from each `CaseListItem` in the list-endpoint
response when the caller's token lacks both `exceptions:approve`
AND `exceptions:override`. Field becomes `None` in the wire payload
— UI's `EvidenceBlock` collapses line 4 to intent badge alone
(Structurally Omitted variant).

### Compliance rationale

Per panel review §4 (Compliance) — verdict color is acceptable
disclosure (already in detail pane). Dollar exposure on cases the
CSA cannot action is materially-sensitive partner-tenant data;
filtered at the API boundary, not the UI.

### Acceptance

1. Integration test: list endpoint called with `cases:read`-only
   token → every `dollar_impact` is null.
2. Integration test: list endpoint called with `exceptions:approve`
   token → `dollar_impact` populated per Ticket 2.
3. Integration test: list endpoint called with `exceptions:override`
   token → `dollar_impact` populated.
4. Architectural lock asserts the filter is applied at the route
   level, not the composer level (so the composer always computes;
   the route decides whether to ship).

### Out of scope

* Filtering `audit_verdict_color` — verdict is acceptable disclosure
  per panel.
* Filtering `problem_one_liner` if it leaks `partner_tenant_data` —
  the recipe templates above use only ASOE-resident analysis fields
  (price/qty/SKU); no echo of partner reason text. Lock asserts
  this by greenfield review.

---

## Sequencing

```
T1 (schema)  →  T2 (composer + recipes)  →  T3 (in-txn dispatch)  →  T4 (RBAC)
   ½ day            3-4 days                     1 day                  ½ day
```

Total Phase 0: ~1 sprint with parallel UI Phase 1 (primitives) running
alongside on the asoe-ui side.

## Sign-off gate to flip `NEXT_PUBLIC_CASES_ROW_V2` ON

1. Tickets 1-4 in production.
2. Compliance reviews rendered-snapshot audit record format for an
   Approve click under the new layout.
3. asoe-ui Phase 2 PR merged behind flag (default OFF).
4. CSA dry-run on 10 sample cases confirms reorder ≥3.
5. Telemetry instrumentation live (row-click, time-to-first-action,
   Analysis scroll depth).
6. **Phase 0b complete** (per-intent template implementations — see
   below). Until then the queue row ships without SKU + one-liner.

---

## Phase 0b follow-on (Recipe team)

Phase 0a shipped the four schema/route/event tickets. The remaining
work is the per-intent template implementations the Recipe SME
panel mapped in their 2026-05-28 response. Owner: Recipe team.
**No blocker for the asoe-ui flag flip** — the projection
gracefully returns null for any intent without a registered
template (Guardrail #6 EvidenceBlock contract on the UI side
collapses the cell).

### T2b — Per-intent template registry

**Scaffold shipped** (`api/case_summary_templates.py`). The
registry, dispatcher, and two working templates (DUPLICATE_PO,
MANUAL_ORDER_INTAKE) plus four grandfather-clause registrations
(PRICE_HOLD, EDI_MISMATCH, PALLET, EMAIL_COMPLAINT) are in `main`
on the asoe2 side. **The remaining nine intents carry
`# TODO(recipe-team)` stubs** with the Recipe SME's verbatim spec
in each function's docstring. Recipe team's owner:

  * Confirm the field names on the matching `*AnalysisData` shape
    against the spec — `asoe-ui/src/types/exceptions.ts` mirrors
    each one.
  * Replace the stub return-None with the template implementation.
  * Add a happy-path + missing-field test mirroring the patterns in
    `tests/test_case_summary_templates.py::TestDuplicatePOTemplate`.
  * When all nine intents are complete, flip the
    `TestTodoStubs::test_todo_template_returns_empty` expectation —
    that lock catches half-written templates today.

Order suggested by the dispatcher docstring (most operator-visible
first): PRICE_DISCREPANCY → BACK_ORDER → CREDIT_BLOCK →
CONTRACTUAL_CORRECTION → MASS_PRICING_ERROR → OVER_MAX → MOQ_UPLIFT
→ DELIVERY_DELAY → CHANGE_ANALYSIS.

Two known recipe-output gaps wired as grandfather-clause no-ops in
the registry today:

  * **PRICE_HOLD** missing `sku` + `qty` on `PriceHoldAnalysisData`
    — either extend the recipe output (Verdict 2026-04-22 Pillar 1)
    or document in `compliance/audit_bearing_registry.yaml::
    grandfather_clauses` with deadline.
  * **EMAIL_COMPLAINT (pure intake)** has no quantity fields — add
    `complaint_analysis` recipe output, or grandfather.

### T2c — Verdict color override gates

Recipe SME §5 — the rollup color today is the raw severity-wins
output of child shadow verdicts. The gates ("never RED at intake
for DELIVERY_DELAY without SLA breach", "never GREEN for
PRICE_HOLD with auto-release near-ceiling", etc.) live in the
recipe layer. T2c adds a `verdict_color_gate(intent, context,
raw_color) -> color` function applied at compose time. Architectural
lock asserts all ten gates from the panel response.

### T2d — Currency carrier field

Today `dollar_impact` defaults to `"USD"` because
`resolution_data["financial_impact_usd"]` is single-currency by
field name. Multi-currency cases need a `currency` field alongside
`financial_impact`. Recipe team to coordinate with Compliance on
the resolution_data field shape before multi-currency tenants land.

### T2e — `top_line_sku_*` multi-line rule

Recipe SME §4 — top line by absolute dollar impact, then `+N more`
suffix when `line_count > 1`. Requires the recipe output to expose
per-line SKU + value (currently only `resolution_data` carries
totals). Recipe team to land per-line breakdown alongside the
template registry.
