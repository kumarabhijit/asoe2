---
name: duplicate-po
description: Detects and classifies Duplicate Purchase Order (PO) exceptions in B2B order intake. Triggered on EDI_850_DUPLICATE_PO events.
metadata:
  version: 1.1.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-sap-connector, mcp-pinecone-search]
  recipes: [DuplicatePORecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Duplicate PO Exception Management

## 1. Overview
This skill is triggered when an inbound EDI 850 order is flagged as a potential
duplicate of an existing PO in the order management system. It classifies the
duplicate probability and routes to the correct recipe or escalation path.
Do not improvise scoring logic. All scoring and thresholds live in `DuplicatePORecipe.py`;
weight overrides are resolved by the `tenant_config` gateway per ADR-029.

## 2. Reasoning Loop
1. Identify that the event is a duplicate PO check (event_type: `EDI_850_DUPLICATE_PO`).
2. Confirm that per-signal match scores are present in `event.metadata.signal_scores`.
3. Classify intent as `DUPLICATE_PO`.
4. Select `DuplicatePORecipe.py` — the only allowed recipe for this intent.
5. Note that orchestration will resolve gateway dependencies before recipe execution:
   `oms.get_fulfillment_status`, `oms.get_matched_po_details`, and
   `tenant_config.resolve_for_event` (the latter supplies the per-event
   weight map per ADR-029, with fail-closed-to-platform on contract
   violation). Do not call any of these gateways directly.
6. Do not call any recipe before the Compliance Shadow has returned GREEN.

## 3. Constrained Generation Policy
Use Guidance / Outlines for all machine-consumed outputs:
- intent label must be `DUPLICATE_PO` (the only valid value for this event type)
- recipe name must be `DuplicatePORecipe.py` (registered recipe, no other allowed)
- shadow verdict must be constrained to GREEN / YELLOW / RED

## 4. Recipe-to-Intent Mapping
- Duplicate PO → `DuplicatePORecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
Before calling any recipe, check the Compliance Shadow.
If the shadow returns RED, halt execution and explain the compliance breach.
If the shadow returns YELLOW, route to MANUAL_REVIEW_REQUIRED.

## 6. Output Requirements
Always include the Recipe Execution Log snippet for audit traceability.
The log must capture: composite_score, classification, recommended_action, signal_breakdown.
The `tenant_config` gateway response (per-layer contribution trace +
validation_status) lives on `state.enrichment_context["tenant_config"]`
and surfaces on the canonical envelope (ADR-028 G2 / A6).
