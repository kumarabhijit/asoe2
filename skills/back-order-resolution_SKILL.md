---
name: back-order-resolution
description: Triaged a back-order / out-of-stock condition on an inbound order line and ranks resolution options — alternate DC, substitute SKU, split shipment, or reschedule. Triggered on BACK_ORDER_OOS events.
metadata:
  version: 1.0.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-oms-connector, mcp-inventory-service]
  recipes: [BackOrderResolutionRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Back-Order (OOS) Resolution

## 1. Overview
This skill is triggered when a line on an inbound order cannot be fully
fulfilled from the primary distribution centre's on-hand inventory. It
classifies the gap severity and ranks resolution options without
inventing business policy — thresholds and option scoring live in
`BackOrderResolutionRecipe.py`.

## 2. Reasoning Loop
1. Confirm event_type is `BACK_ORDER_OOS`.
2. Read `metadata.ordered_qty` and `metadata.available_qty`. Never
   guess — if either is missing, classification fails to human.
3. Classify intent as `BACK_ORDER`.
4. Select `BackOrderResolutionRecipe.py` — the only allowed recipe.
5. Compliance Shadow must return a verdict before the recipe runs.

## 3. Constrained Generation Policy
- intent label must be `BACK_ORDER`
- recipe name must be `BackOrderResolutionRecipe.py`
- shadow verdict must be `GREEN | YELLOW | RED`
- resolution option `type` field emitted verbatim from the recipe —
  not branched on by orchestration.

## 4. Recipe-to-Intent Mapping
- BACK_ORDER → `BackOrderResolutionRecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
Compliance Shadow runs first. Verdicts:
- GREEN → variance within auto-handle band; recipe proposal presented.
- YELLOW → MINOR_GAP; review the ranked options and approve.
- RED → SEVERE_GAP (gap ≥ `BACK_ORDER_SEVERE_GAP_PCT`); escalate to
  buyer decision. Hard-block applies until escalation completes.

## 6. Output Requirements
Recipe Execution Log must capture:
- `gap_qty` and `gap_pct`
- `classification` (NO_GAP | MINOR_GAP | SEVERE_GAP)
- `primary_option` (top-ranked) + full `resolution_options` list
- `recommended_action` (SPLIT_SHIPMENT | ALT_DC | SUBSTITUTE | RESCHEDULE)
- `at_risk` (gap × unit_price)
