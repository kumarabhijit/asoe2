---
name: moq-round-up
description: Decides whether to round a PO line up to the Minimum Order Quantity, accept below MOQ, or escalate for a KNMT waiver. Triggered on MIN_ORDER_QTY events.
metadata:
  version: 1.0.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-sap-connector]
  recipes: [MOQRoundUpRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Minimum Order Quantity (MOQ) Round-Up

## 1. Overview
Triggered when a PO line's ordered quantity falls below the
KNMT-MINBM minimum. The skill classifies shortfall severity and
recommends ROUND_UP, ACCEPT_BELOW, or ESCALATE. All thresholds
(severe shortfall, uplift review) live in `MOQRoundUpRecipe.py`.

## 2. Reasoning Loop
1. Confirm event_type is `MIN_ORDER_QTY`.
2. Read `metadata.ordered_qty`, `metadata.moq_qty`, and
   `metadata.unit_cost`.
3. Classify intent as `MIN_ORDER_QTY`.
4. Select `MOQRoundUpRecipe.py`.
5. Compliance Shadow runs before the recipe.

## 3. Constrained Generation Policy
- intent must be `MIN_ORDER_QTY`
- recipe must be `MOQRoundUpRecipe.py`
- shadow verdict `GREEN | YELLOW | RED`
- recipe `recommended_action` constrained to
  `ROUND_UP | ACCEPT_BELOW | ESCALATE | NO_ACTION`

## 4. Recipe-to-Intent Mapping
- MIN_ORDER_QTY → `MOQRoundUpRecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
- GREEN: ordered ≥ MOQ; no shortfall.
- YELLOW: MINOR shortfall within round-up band; operator confirms the
  round-up before execution. When the uplift value exceeds
  `MOQ_UPLIFT_REVIEW_PCT`, even MINOR cases require sign-off.
- RED: SEVERE shortfall at/above `MOQ_SEVERE_SHORTFALL_PCT`; KNMT
  waiver or sales-manager approval mandatory.

## 6. Output Requirements
- `shortfall_qty`, `shortfall_pct`, `uplift_qty`, `uplift_pct`, `uplift_value`
- `classification` (NO_SHORTFALL | MINOR_SHORTFALL | SEVERE_SHORTFALL)
- `round_up_plan` (sku, ordered, round_up_to, delta, action, reason)
- `recommended_action` (ROUND_UP | ACCEPT_BELOW | ESCALATE | NO_ACTION)
