---
name: pallet-alignment
description: Detects broken-layer (SD-PLT-001) and partial-pallet (SD-PLT-002) violations and proposes a round-down plan aligned to full layers / full pallets. Triggered on PALLET_CONFIG_VIOLATION events.
metadata:
  version: 1.0.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-sap-connector, mcp-logistics-calc]
  recipes: [PalletAlignmentRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Pallet Configuration Alignment

## 1. Overview
Pallet violations arise when ordered quantities don't align to full
layers / full pallets, producing freight waste and labour overhead.
The skill identifies broken-layer and partial-pallet conditions and
proposes a round-down plan. Threshold fractions are policy-injected
into `PalletAlignmentRecipe.py`.

## 2. Reasoning Loop
1. Confirm event_type is `PALLET_CONFIG_VIOLATION`.
2. Read `metadata.pallet_lines` — each entry carries `sku`,
   `layer_qty`, `pallet_qty`, `ordered_qty`.
3. Classify intent as `PALLET_CONFIG`.
4. Select `PalletAlignmentRecipe.py`.
5. Compliance Shadow always returns YELLOW when pallet data is
   present — operator sign-off is required because the round-down
   reduces ordered quantity.

## 3. Constrained Generation Policy
- intent must be `PALLET_CONFIG`
- recipe must be `PalletAlignmentRecipe.py`
- shadow verdict `GREEN | YELLOW | RED`
- recipe classification constrained to
  `NO_VIOLATION | BROKEN_LAYER | PARTIAL_PALLET | MIXED_VIOLATION`

## 4. Recipe-to-Intent Mapping
- PALLET_CONFIG → `PalletAlignmentRecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
Pallet-config violations never hard-block (no RED from policy hits
alone). They always enter the YELLOW review queue so an operator can
confirm the round-down and communicate reduced quantities to the
buyer.

## 6. Output Requirements
- `total_ordered_cases`, `loose_cases_total`, `order_line_count`
- `classification`
- `lines[]` with `complete_layers`, `loose_qty`, `full_pallets`,
  `pallet_fill_pct`, `violation_type`
- `suggested_plan[]` with `current`, `suggested`, `delta`, `reason`
- `recommended_action` (ROUND_DOWN_TO_LAYERS | ACCEPT_AS_IS | REVIEW)
