---
name: edi-mismatch
description: Classifies non-duplicate EDI 850 line mismatches (SKU, qty, UOM, ship-to) and selects the appropriate resolution path. Triggered on EDI_850_LINE_MISMATCH events whose metadata.mismatch_sub_type is not PRICE_MISMATCH.
metadata:
  version: 1.0.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-buyer-notification]
  recipes: [EdiMismatchRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: EDI Line Mismatch

## 1. Overview
This skill handles EDI 850 line-level mismatches against the master record:
SKU, quantity, unit-of-measure, and ship-to. Each sub_type has a
deterministic classification branch in `EdiMismatchRecipe.py` — SKU
mismatches hard-reject, qty / UOM require buyer confirmation, and ship-to
escalates.

`PRICE_MISMATCH` is **not** handled here. An EDI 850 line mismatch whose
field is `price` is re-routed at classifier time to `CONTRACTUAL_CORRECTION`
and executes `PriceAdjustmentRecipe.py`, keeping pricing the single source
of truth (CLAUDE.md §1). The skill loader and the deterministic-fallback
classifier both fork on `event.metadata.mismatch_sub_type` so a PRICE
mismatch is statically impossible to reach this skill.

## 2. Reasoning Loop
1. Confirm event_type is `EDI_850_LINE_MISMATCH`.
2. Read `event.metadata.mismatch_sub_type`. Never infer the sub_type — if
   missing, fail to human.
3. Confirm `mismatch_sub_type ∈ AllowedEdiMismatchSubType` (SKU, QTY, UOM,
   SHIP_TO). If equal to `PRICE_MISMATCH`, this skill should never have been
   loaded — raise a routing-invariant failure.
4. Classify intent as `EDI_MISMATCH`.
5. Select `EdiMismatchRecipe.py` — the only allowed recipe for this intent.
6. Do not call the recipe before the Compliance Shadow has returned a verdict.

## 3. Constrained Generation Policy
Use Guidance / Outlines for all machine-consumed outputs:
- intent label must be `EDI_MISMATCH`
- recipe name must be `EdiMismatchRecipe.py`
- `sub_type` constrained to `AllowedEdiMismatchSubType`
- recipe output `classification` constrained to `AllowedEdiMismatchClassification`
  (HARD_REJECT / REVIEW / ESCALATE)
- shadow verdict constrained to GREEN / YELLOW / RED

## 4. Recipe-to-Intent Mapping
- EDI_MISMATCH → `EdiMismatchRecipe.py`
- `mismatch_sub_type == PRICE_MISMATCH` → handled by `PriceAdjustmentRecipe.py`
  via the CONTRACTUAL_CORRECTION path (routed at classifier time).

## 5. Execution Protocol
Compliance Shadow runs before the recipe. Verdicts are sub_type-aware:
- SKU_MISMATCH → RED (order cannot fulfil as received).
- SHIP_TO_MISMATCH → YELLOW (escalate).
- QTY_MISMATCH / UOM_MISMATCH → YELLOW (request buyer confirmation).

The recipe itself is pure classification — no I/O, no side effects. The
downstream buyer-notification effect is applied by orchestration after the
recipe returns, not by the recipe.

## 6. Output Requirements
The Recipe Execution Log must capture:
- `sub_type`
- `classification` (HARD_REJECT | REVIEW | ESCALATE)
- `recommended_action` (BLOCK_AND_NOTIFY | REQUEST_BUYER_CONFIRMATION | ESCALATE)
- `autonomy_level` (L1 / L2 / L3)
- `expected_value` and `received_value` (for audit)
