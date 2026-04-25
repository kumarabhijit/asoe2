# ADR-025: Move gateway READS before shadow_audit so audit evidence is captured for every record

**Status:** Accepted
**Date:** 2026-04-25
**Deciders:** Principal AI Systems Architect; Compliance review pending
**Applies to:** `orchestration/graph.py`, `orchestration/nodes.py`,
`contracts/models.py`, `tests/test_e2e_om_adjacent_intents.py`,
`tests/test_api.py`, `tests/test_nodes.py`, `tests/test_golden.py`

---

## Context

The Verdict full-close engagement (2026-04-22) retired all four
grandfather clauses
(`price_analysis_gateway_gap`,
`delivery_delay_financial_gap`,
`overmax_gateway_gap`,
`moq_gateway_gap`) and shipped four new adapters covering the
previously-mock-only enrichment sections. With clauses retired, the
composer in `build_analysis` enforces audit-bearing coverage on every
record. Verdict Pillar 1 is explicit: gateway-fetched evidence
(matched POs, warehouse snapshots, contract refs, SAP doc numbers,
SLA contracts) must be captured at exception time, persisted to
`enrichment_context`, and surfaced to the operator alongside the
record.

After the clause retirements landed, an emergent failure mode showed
up in the e2e tests: every shadow-gated OVER_MAX / MIN_ORDER_QTY /
DELIVERY_DELAY / BACK_ORDER record was routing to
`AUDIT_CONTEXT_MISSING`. Tracing it back: the live graph runs gateway
READS (`resolve_dependencies`) only after `shadow_audit` returns
GREEN. Shadow-gated records (RED → BLOCKED, YELLOW →
MANUAL_REVIEW_REQUIRED) terminate before `resolve_dependencies` ever
fires, so `enrichment_context` is empty when `build_analysis` runs
and the registry-required gateway fields are missing.

The architectural question: is the current ordering (shadow before
gateway READS) load-bearing, or convention?

## Decision

**Move `resolve_dependencies` before `shadow_audit`.** The new live-mode
sequence:

```
ingest → classify → load_skill → validate_circuit_breaker
  ├─[breach]→ build_analysis (FAIL_TO_HUMAN)
  └─[ok]→ select_recipe
       ├─[no recipe]→ shadow_audit (continues — see "Recipe-less intents" below)
       └─[ok]→ resolve_dependencies
            ├─[required-gw fail]→ build_analysis (FAIL_TO_HUMAN)
            └─[ok / soft-fail]→ validate_types
                 ├─[invocation fail]→ build_analysis
                 └─[ok]→ shadow_audit
                      ├─[RED]→ build_analysis (BLOCKED, with audit evidence)
                      ├─[YELLOW]→ build_analysis (MANUAL_REVIEW_REQUIRED, with audit evidence)
                      └─[GREEN]→ execute_recipe → apply_effects → build_analysis (COMPLETE)
```

Explain-mode sequence is identical except the `[GREEN]` branch routes
to `explain_only → build_analysis` instead of executing the recipe.

### Why this is consistent with CLAUDE.md Guardrail #4

Guardrail #4 mandates: *"Before any recipe execution: propose the
action, run Compliance Shadow, interpret the verdict, continue only
if policy allows."* The load-bearing word is **execution**. Shadow's
job is to gate the recipe run and any external write effects, not to
gate data acquisition. The current code over-couples reads to that
gate as a latency optimisation ("why fetch if we won't execute?")
rather than a compliance invariant.

The reorder respects the guardrail strictly: `shadow_audit` still
runs immediately before `execute_recipe`, with a fully-materialised
proposal (intent + recipe + resolved evidence + validated params) to
audit against. Shadow now sees a *richer* proposal than before.

### Why classify is OK to run before shadow

`classify` runs without external queries — it operates on the event
payload that arrived in the request body. Whoever ingested that event
(EDI handler, SAP polling job, OMS CDC) did their reads upstream of
ASOE2. There was never a "shadow before any data touch" invariant;
the current shadow-before-resolve_dependencies sequence is the only
place that approximation showed up, and it was always implicit.

## Auxiliary changes

1. **`request_trace_id` on `GraphState`.** Stamped at `ingest`
   (UUID). Used by `resolve_dependencies` and `apply_effects` to tag
   gateway calls. Distinct from `shadow.trace_id` (the
   ComplianceDecision row ID, generated when shadow runs). Necessary
   because `resolve_dependencies` no longer has shadow.trace_id when
   it runs.

2. **`required_for_audit: bool` on `GatewayDependency`.** Default
   `True`. Controls failure semantics in `resolve_dependencies`:
   * `True` (strict): gateway failure halts with `FAIL_TO_HUMAN`.
   * `False` (soft): gateway failure logs and writes an empty dict
     to `enrichment_context[result_key]`; the composer's coverage
     check then routes to `AUDIT_CONTEXT_MISSING` if the absent
     fields turn out to be required, otherwise the run proceeds.
   This lets recipes tune strictness per gateway — useful if a
   particular SAP integration is flaky and operators would rather
   review the record without that piece of evidence than have the
   record halted.

3. **`select_recipe` no longer terminates on no-recipe.** Recipe-less
   intents (e.g. `MASS_PRICING_ERROR`, `UNKNOWN`) previously raised
   `FAIL_TO_HUMAN` at `select_recipe`. With shadow now downstream,
   that short-circuited shadow's authority to be the terminal voice
   for compliance-only outcomes. New behaviour: `select_recipe`
   leaves `selected_recipe = None` and sets a diagnostic
   `explanation`; if shadow returns RED/YELLOW, the record terminates
   appropriately; if shadow returns GREEN, `execute_recipe` raises
   `FAIL_TO_HUMAN` with the upstream explanation preserved.

## Consequences

### Positive

* **Verdict Pillar 1 reachable for every record.** Shadow-gated
  records (RED/YELLOW) now carry full audit evidence. Operators
  reviewing a `MANUAL_REVIEW_REQUIRED` record see the warehouse
  snapshot, contract reference, SLA deadline, etc. — they're not
  asked to authorise an action with empty enrichment_context.
* **Pre-existing ordering quirk fixed.** Post-T1, `validate_types`
  was reading from `state.enrichment_context` to build recipe
  invocation params (DuplicatePO `original_fulfilled` /
  `has_revision_indicator` / `line_items_identical`; PriceHoldRelease
  `hold_status`), but `resolve_dependencies` ran AFTER
  `validate_types` so the bag was empty. Recipes silently ran with
  `None` params. The new sequence fixes this — invocations are
  fully resolved.
* **Shadow sees a richer proposal.** Shadow currently uses only
  `intent` + `event` for its policy_hits, but future policies could
  read `state.invocation.params` or `state.enrichment_context` (e.g.
  *"block when contract_context.customer_is_on_credit_hold"*) without
  re-ordering the graph.
* **Live and explain graphs converge.** Both run the same
  `resolve_dependencies → validate_types → shadow_audit` common
  section; only the post-shadow GREEN continuation differs.
* **Test surface simplifies.** No more split assertions ("live →
  COMPLETE, shadow-gated → AUDIT_CONTEXT_MISSING"). Shadow-gated
  records cleanly route to BLOCKED/MANUAL_REVIEW_REQUIRED with
  populated evidence.

### Negative

* **Gateway calls fire on RED-blocked records.** Previously, RED
  shadow halted before any gateway READ. Now reads run on every
  exception that gets past `validate_circuit_breaker` and
  `select_recipe`. Cost: ~100–300ms of latency per shadow-gated path
  for the current recipe set, plus pressure on SAP-API quotas in
  production. Mitigation: the `required_for_audit` flag lets recipes
  mark expensive non-essential gateways soft-fail-tolerant; future
  optimisation can introduce per-gateway gating if cost becomes
  material.
* **Auditor narrative shifts.** Audit logs now show "evidence fetched
  before compliance verdict" for RED-blocked records. The narrative:
  *evidence acquisition is deterministic and replayable; it happens
  for every exception so the audit trail is complete regardless of
  outcome*. Documented here and in `_add_common_nodes_and_edges`
  docstring.
* **`select_recipe` semantics shifted.** Unit tests that pinned
  `final_status == FAIL_TO_HUMAN` after `select_recipe` for
  recipe-less intents now expect `final_status is None` (terminal
  routing happens later). Two tests in `test_nodes.py` updated.

### Neutral

* `validate_circuit_breaker` placement unchanged. It halts on input
  anomalies (high `update_count`, batch variance) and is about
  rate-limiting ASOE2's own behaviour — independent of the
  shadow-vs-reads ordering.
* Kill switch (`hardening/kill_switch.py`) unchanged. It still
  short-circuits before any node executes when `ASOE_KILL_SWITCH=1`.

## Related decisions

* Verdict three-pillar architecture (2026-04-22 compliance workshop):
  Pillar 1 (`enrichment_context` persistence), Pillar 2
  (`build_analysis` registry enforcement), Pillar 3 (UI
  EvidenceBlock). This ADR makes Pillar 1 reachable on every record.
* T1-T5 grandfather clause retirements (commits aaf0ec1 → f830faa).
* CLAUDE.md Guardrail #4 (Compliance Shadow is mandatory before
  recipe execution) — reinterpreted strictly per its load-bearing
  scope ("execution and effects", not "data acquisition").
