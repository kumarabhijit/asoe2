---
name: over-max-trim
description: Classifies a PO whose total quantity exceeds the contract max and recommends a per-line trim plan that preserves pallet integrity where possible. Triggered on OVER_MAX_QTY events.
metadata:
  version: 1.0.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-sap-connector]
  recipes: [OverMaxTrimRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Over-Max Quantity Trim

## 1. Overview
Triggered when the sum across PO lines exceeds the customer's
contract-max quantity. The skill classifies the exceedance severity
and builds a per-line trim plan with TRIM / SKIP / OK actions.
Thresholds and allocation logic live in `OverMaxTrimRecipe.py`.

## 2. Reasoning Loop
1. Confirm event_type is `OVER_MAX_QTY`.
2. Read `metadata.total_ordered`, `metadata.max_qty`, and
   `metadata.order_lines`.
3. Classify intent as `OVER_MAX`.
4. Select `OverMaxTrimRecipe.py`.
5. Compliance Shadow runs before the recipe.

## 3. Constrained Generation Policy
- intent must be `OVER_MAX`
- recipe must be `OverMaxTrimRecipe.py`
- shadow verdict `GREEN | YELLOW | RED`
- per-line action constrained to `TRIM | SKIP | OK`

## 4. Recipe-to-Intent Mapping
- OVER_MAX → `OverMaxTrimRecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
- GREEN: no exceedance — no trim required.
- YELLOW: MINOR exceedance (within `OVER_MAX_SEVERE_EXCEEDANCE_PCT`);
  operator confirms the trim plan before apply.
- RED: SEVERE exceedance; sales-manager escalation is mandatory.

Broken-layer lines (non-even pack) get `action=SKIP` — the skill
surfaces these to the human instead of auto-trimming and creating
pallet fragmentation.

## 6. Output Requirements
- `excess_qty`, `exceedance_pct`, `at_risk`
- `classification` (NO_EXCEEDANCE | MINOR | SEVERE)
- Full `trim_plan` (per-line action + delta + reason)
- `recommended_action` (TRIM_TO_MAX | ESCALATE | NO_ACTION)
