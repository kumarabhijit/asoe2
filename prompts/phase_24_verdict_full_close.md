# Phase 24 — Verdict Full-Close (retire all grandfather clauses + ADR-025 graph reorder)

```text
Read CLAUDE.md (esp. Guardrail #6 — UI richness as product
commitment), tasks.md (Phase 23), docs/adr/ADR-025-gateway-reads-
before-shadow.md, and compliance/audit_bearing_registry.yaml.
Implement only Phase 24 (the close-out of the Verdict three-pillar
architecture started in Phase 23).

Phase 23 shipped 6 of 10 enrichment sections backend-backed plus
the three-pillar scaffolding (enrichment_context persistence,
build_analysis registry enforcement, EvidenceBlock UI primitive).
Four sections remained mock-only (price_analysis,
duplicate_detection, order_comparison, backorder_analysis), and
four grandfather clauses were still active
(price_analysis_gateway_gap, delivery_delay_financial_gap,
overmax_gateway_gap, moq_gateway_gap).

Phase 24 closes the Verdict commitment: every audit-bearing field
in the registry persists end-to-end via real (or sandbox-stubbed)
gateway reads. No grandfather clauses remain. The graph topology
is reordered (ADR-025) so audit-bearing evidence is captured for
every record, including shadow-gated ones.

Requirements:

T1 — Single-bag enrichment_context (foundation):
  * resolve_dependencies writes only to state.enrichment_context;
    drop the resolved_data dual-write.
  * Recipe-input nodes read from enrichment_context too (one bag).
  * V004 migration adds enrichment_context as a durable JSONB
    column on `exceptions`; runner.py applies it for SQLite +
    Postgres.
  * DbExceptionStore.create() threads enrichment_context to the
    repository; drop the in-memory bridge (lines 467-472 pre-T1).
  * `_persist_exception` drops the resolved_data → enrichment_context
    fallback. Single source of truth.

T2 — DuplicatePO + OrderComparison adapters:
  * api/schemas.py — OrderSnapshot, DuplicateDetectionData,
    ComparisonOrder, ComparisonLineItem, OrderComparisonData
    Pydantic models.
  * api/analysis_adapters.py — adapt_duplicate (primary, audit
    enforcement target) + adapt_order_comparison (secondary,
    synthesised from same matched_po_details payload).
  * SECONDARY_ANALYSIS_ADAPTERS registry — new pattern for derived
    projections that share the primary's attestation target.
  * INTENT_TO_RECIPE_NAME["DUPLICATE_PO"] = "DuplicatePORecipe.py".
  * adapt_duplicate has a synthesis fallback (pure recipe call)
    for explain-mode + shadow-gated paths.
  * Explain graph wires resolve_dependencies (gateway READS are
    safe in dry-run) so explain-mode audit enforcement sees the
    same evidence the live path would.

T3 — BackOrder adapter:
  * api/schemas.py — WarehouseInfo, AlternateWarehouse,
    SubstituteSKU, InboundOrder, ResolutionOption,
    BackOrderAnalysisData.
  * api/analysis_adapters.py — adapt_back_order with synthesis
    fallback.
  * INTENT_TO_RECIPE_NAME["BACK_ORDER"] = recipe.

T4 — Price adapter + retire price_analysis_gateway_gap:
  * recipes/registry.py — PriceAdjustmentRecipe gains 3
    GatewayDependency entries (sap_doc, sap_contract, promotion).
  * api/schemas.py — PriceAnalysisData with audit-bearing fields
    populated from sap_doc_context / contract_context /
    promotion_context.
  * Compliance: retire price_analysis_gateway_gap in
    audit_bearing_registry.yaml; reclassify contract_ref +
    promotion_ref from audit-bearing → contextual (conditionally
    present, not every line is contract- or promotion-governed).
  * build_analysis() preserves FAIL_TO_HUMAN against
    AUDIT_CONTEXT_MISSING override (circuit breaker / validation
    failures stay debuggable).

T5 — Retire delivery_delay_financial_gap / overmax_gateway_gap /
moq_gateway_gap:
  * recipes/registry.py — DeliveryDelayResolutionRecipe,
    OverMaxTrimRecipe, MOQRoundUpRecipe each gain
    GatewayDependency entries (sla_contract / sap_contract +
    sap_block / sap_customer_master + sap_contract + sap_block).
  * api/analysis_adapters.py — adapt_delivery_delay /
    adapt_overmax / adapt_moq extended to project the
    previously-grandfathered fields from the new gateway result
    keys; metadata fallback retained for shadow-gated paths.
  * Compliance: retire all three clauses;
    grandfather_clauses block in registry.yaml is now empty.

ADR-025 — Gateway READS before shadow_audit:
  * Move resolve_dependencies before shadow_audit so audit-bearing
    evidence is captured for every record (RED/YELLOW/GREEN).
  * Common section ends at shadow_audit's conditional edge; each
    graph variant adds its own post-shadow GREEN continuation
    (execute_recipe + apply_effects in live; explain_only in
    explain).
  * GraphState.request_trace_id (UUID stamped at ingest) replaces
    state.shadow.trace_id for gateway-call correlation in
    resolve_dependencies (shadow hasn't run yet).
  * GatewayDependency.required_for_audit (bool, default True) —
    soft-fail path for non-essential gateway reads. Strict
    halts FAIL_TO_HUMAN; soft writes empty dict and lets the
    composer route to AUDIT_CONTEXT_MISSING via the standard
    coverage check.
  * select_recipe no longer terminates on no-recipe — shadow gets
    to be the terminal voice for compliance-only intents
    (MASS_PRICING_ERROR / UNKNOWN). On shadow GREEN,
    execute_recipe's invocation-None guard preserves the
    select_recipe explanation when raising FAIL_TO_HUMAN.

Sandbox infrastructure:
  * api/sandbox_gateways.py (new) — register_sandbox_gateways()
    mirrors tests/conftest.py StubGateways: oms,
    buyer_notification, sap_doc, sap_contract, promotion,
    sap_block, sap_customer_master, sla_contract. Idempotent.
    Called from create_app() inside the ASOE_ENV=sandbox block.
  * db/migrations/runner.py — drop the V1 `intent` CHECK
    constraint on the SQLite schema (Intent enum at
    contracts/models.py is the source of truth; CHECK drifted
    every time a new intent shipped).

Tests:
  * Per-tranche adapter tests: test_analysis_adapters_duplicate.py
    (14 cases), test_analysis_adapters_back_order.py (10),
    test_analysis_adapters_price.py (14),
    test_analysis_adapters_t5.py (8 — covers the 3 retired
    clauses + their gateway projections).
  * Single-bag semantics: test_enrichment_context.py extended.
  * E2E: test_e2e_om_adjacent_intents.py reverts shadow-gated
    paths to BLOCKED / MANUAL_REVIEW_REQUIRED expectations
    (post-ADR-025, audit evidence lands even on shadow-gated
    records, so the composer no longer routes to
    AUDIT_CONTEXT_MISSING).

Suite at end of phase: 1343 passed, 35 skipped (was 1291 + 35
pre-engagement; +52 net new tests across the 5 tranches).

Definition of done:
  * grandfather_clauses block in audit_bearing_registry.yaml is
    empty (all 4 clauses retired).
  * 10 of 10 enrichment sections backend-backed (asoe-ui D18
    flips PARTIAL 6/10 → SHIPPED 10/10 in companion phase 8.12).
  * Both build_graph() and build_explain_graph() compile and
    invoke cleanly with the new node ordering.
  * Sandbox local-dev e2e walkthrough works end-to-end (curl one
    event per intent through /api/v1/exceptions/resolve, verify
    every adapter populates including YELLOW shadow records).
  * Companion ADR (docs/adr/ADR-025) lands documenting the
    reorder, the Guardrail #4 reinterpretation, and the
    consequences (gateway calls fire on RED-blocked records;
    auditor narrative shifts).
```

## See also

- `docs/adr/ADR-025-gateway-reads-before-shadow.md` — full
  architectural rationale.
- `compliance/audit_bearing_registry.yaml` — post-engagement state
  (grandfather_clauses block empty).
- `api/sandbox_gateways.py` — runtime stub registration.
- `asoe-ui/prompts/phase_8_12_verdict_ui_sync.md` — UI-side
  companion.
