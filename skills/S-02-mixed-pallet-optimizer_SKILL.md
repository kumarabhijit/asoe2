---
name: s-02-mixed-pallet-optimizer
description: >
  AI-powered mixed-SKU pallet optimization for ASOE. Given candidate line groups from R-06 Cross-Line
  Detector, determines the optimal pallet build plan by interleaving multiple SKUs across layers. Handles
  combinatorial complexity when 3+ broken lines with different Ti values, weight constraints, and
  stackability rules must be combined. Outputs a layer-by-layer pallet build, freight savings analysis,
  and surcharge elimination calculation. Triggers when R-06 returns eligible=true and S-01 decides
  optimization is worth pursuing. AI reasoning required for multi-line trade-off analysis and build
  plan generation.
---

# S-02 — Mixed Pallet Optimizer

## 1. Problem domain

When multiple broken lines on the same order can be physically combined onto a shared pallet, the
warehouse ships fewer pallets, freight costs drop, and broken-layer surcharges may be eliminated entirely.
For simple two-line combinations (same Ti, same case dimensions), the math is trivial. But real orders
often have 3-5+ broken lines with different Ti values (8, 10, 12), different case weights pushing against
the 1000kg pallet limit, and stackability constraints (heavy cases on bottom, fragile on top).

This creates a bin-packing optimization problem with constraints — a domain where AI reasoning adds
genuine value over deterministic rules.

### Why this is a skill, not a recipe

- Two lines with identical Ti and dimensions → recipe could handle this (combine, check if
  combined qty mod Ti = 0, done). But the orchestrator can call this skill even for simple cases
  to get the savings analysis and pallet build plan.
- Three+ lines with different Ti values → the agent must decide which lines to group, which Ti
  to use as the base for mixed layers, and whether a mixed layer (e.g., 3 cases of SKU-A + 7 cases
  of SKU-B on a Ti=10 layer) is structurally sound.
- Weight constraints → a pallet of 50 cases at 7.8 kg/case = 390 kg (fine), but adding a second
  SKU at 6.2 kg/case changes the weight distribution. The agent checks per-layer and total weight.
- Trade-off reasoning → sometimes a mixed pallet with 80% fill is better than two separate pallets
  at 50% fill each. Sometimes it's not — if the warehouse charges $8 per mixed-pallet handling and
  the freight savings are only $6, the optimization is net negative.

## 2. Inputs

```json
{
  "candidate_groups": [
    {
      "group_id": "GRP-001",
      "family": "BEV-CSD-COLA",
      "lines": [
        {
          "line_id": "000010",
          "matnr": "000000000000050042",
          "maktx": "12-pk Cola 355ml",
          "qty": 23,
          "ti": 10,
          "case_weight_kg": 4.08,
          "case_length_mm": 400,
          "case_width_mm": 267,
          "case_height_mm": 178,
          "unit_price": 14.88,
          "standalone_surcharge": 3.57
        },
        {
          "line_id": "000020",
          "matnr": "000000000000050043",
          "maktx": "12-pk Diet Cola 355ml",
          "qty": 17,
          "ti": 10,
          "case_weight_kg": 3.95,
          "case_length_mm": 400,
          "case_width_mm": 267,
          "case_height_mm": 178,
          "unit_price": 14.88,
          "standalone_surcharge": 8.33
        }
      ],
      "combined_qty": 40,
      "combined_ti": 10,
      "combined_mod_ti": 0,
      "dimension_match": "EXACT"
    }
  ],
  "pallet_constraints": {
    "max_pallet_weight_kg": 1000,
    "max_stack_height_mm": 1524,
    "pallet_type": "GMA"
  },
  "customer_policy": {
    "mixed_sku_pallet_allowed": true,
    "mixed_family_only": false
  },
  "warehouse_config": {
    "mixed_pallet_handling_cost_usd": 8.00,
    "estimated_freight_per_pallet_usd": 45.00
  }
}
```

## 3. Optimization logic

### Step 1 — Sort lines by quantity descending
Largest qty line becomes the "base" SKU on the pallet. This minimizes the number of mixed layers.

### Step 2 — Build layers bottom-up
```
FOR each layer (1 to max_layers):
    IF remaining_qty_of_current_sku >= ti:
        FILL full layer with current SKU
    ELSE IF remaining_qty_of_current_sku > 0:
        # Mixed layer: fill remainder with current SKU, then next SKU
        mixed_layer = {
            sku_a: remaining_qty_of_current_sku,
            sku_b: ti - remaining_qty_of_current_sku
        }
        ADVANCE to next SKU
    ELSE:
        ADVANCE to next SKU and fill full layer
```

### Step 3 — Validate constraints per pallet
```
total_weight = SUM(layer.cases × layer.case_weight for each layer)
total_height = SUM(layer.case_height for each layer) + pallet_base_height

IF total_weight > max_pallet_weight_kg: SPLIT into 2 pallets
IF total_height > max_stack_height_mm: REDUCE layers, start new pallet
```

### Step 4 — Calculate savings
```
pallets_standalone = SUM(CEIL(line.qty / line.full_pallet_qty) for each line)
pallets_optimized  = number of pallets in the build plan

pallets_saved = pallets_standalone - pallets_optimized
freight_savings = pallets_saved × estimated_freight_per_pallet
surcharge_eliminated = SUM(line.standalone_surcharge for lines in group)
handling_cost = pallets_optimized × mixed_pallet_handling_cost (only if pallet has mixed layers)
net_savings = freight_savings + surcharge_eliminated - handling_cost
```

### Step 5 — Recommendation (AI judgment)
The agent evaluates whether the optimization is worth it:
- net_savings > 0 → recommend optimization
- net_savings ≤ 0 but improves pallet fill rate by 20%+ → recommend (long-term efficiency)
- net_savings ≤ 0 and marginal fill improvement → do not recommend, keep standalone

## 4. Outputs

```json
{
  "recommended": true,
  "recommendation_reason": "Mixed pallet consolidation saves 1 pallet position, eliminates $11.90 in surcharges, and reduces freight by $45. Net savings after $8 handling cost: $48.90.",
  "proposed_pallets": [
    {
      "pallet_id": "MXP-001",
      "type": "MIXED",
      "layers": [
        { "layer": 1, "sku": "000000000000050042", "cases": 10, "weight_kg": 40.80 },
        { "layer": 2, "sku": "000000000000050042", "cases": 10, "weight_kg": 40.80 },
        { "layer": 3, "sku_a": "000000000000050042", "cases_a": 3, "sku_b": "000000000000050043", "cases_b": 7, "weight_kg": 39.89, "mixed": true },
        { "layer": 4, "sku": "000000000000050043", "cases": 10, "weight_kg": 39.50 }
      ],
      "total_cases": 40,
      "total_weight_kg": 161.0,
      "total_layers": 4,
      "fill_pct": 80,
      "mixed_layer_count": 1,
      "weight_ok": true,
      "height_ok": true
    }
  ],
  "savings": {
    "pallets_before": 2,
    "pallets_after": 1,
    "pallets_saved": 1,
    "freight_savings_usd": 45.00,
    "surcharge_before_usd": 11.90,
    "surcharge_after_usd": 0,
    "surcharge_savings_usd": 11.90,
    "handling_cost_usd": 8.00,
    "net_savings_usd": 48.90
  },
  "lines_affected": ["000010", "000020"],
  "override_per_line_actions": {
    "000010": { "new_action": "PASS", "reason": "Absorbed into mixed pallet MXP-001" },
    "000020": { "new_action": "PASS", "reason": "Absorbed into mixed pallet MXP-001" }
  }
}
```

## 5. Complex scenario: 4 lines, different Ti values

```
Line 10: 23 CS of SKU-A (Ti=10, 4.08 kg/case)   → 2 full layers + 3 broken
Line 20: 17 CS of SKU-B (Ti=10, 3.95 kg/case)    → 1 full layer + 7 broken
Line 30: 15 CS of SKU-C (Ti=8,  6.20 kg/case)    → 1 full layer + 7 broken
Line 40: 9  CS of SKU-D (Ti=8,  5.10 kg/case)    → 1 full layer + 1 broken
```

Agent reasoning:
- Lines 10+20 share Ti=10 and same case dimensions → group A (combined 40, mod 10 = 0)
- Lines 30+40 share Ti=8 and similar dimensions → group B (combined 24, mod 8 = 0)
- Group A fills 4 layers on pallet 1. Group B fills 3 layers on pallet 2.
- Alternative: put all on one pallet? 40+24 = 64 cases. But Ti=10 and Ti=8 layers can't mix
  (different footprints). Keep separate.
- Recommendation: 2 pallets (down from 4 standalone). Freight savings: $90. Surcharges eliminated.

## 6. Edge cases

| Case | Behavior |
|---|---|
| Combined weight exceeds pallet max | Split into 2 optimized pallets. Still better than 3+ standalone. |
| Combined height exceeds stack max | Reduce layers per pallet. May need additional pallet. |
| Only 2 lines, identical Ti and dims | Trivial case — build plan is deterministic. Still run savings calc. |
| Mixed layer has weight imbalance | Put heavier SKU on bottom layers. Flag if >20% imbalance. |
| Net savings negative | Recommend against optimization. Return standalone actions unchanged. |
| Warehouse doesn't support mixed pallets | Should be caught by R-06 — but if it reaches S-02, reject. |

## 7. Dependencies

- Upstream: R-06 (candidate groups), R-01 (pallet math per line), R-05 (standalone surcharge amounts).
- Called by: S-01 Orchestrator (conditionally, when R-06 reports eligible groups).
- Downstream: Results feed back to S-01 to override per-line actions. S-03 may generate optimization
  notification if the customer should be informed of the mixed-pallet approach.

## 8. Performance

AI call: 1-3 seconds (single LLM invocation for build plan generation and savings analysis).
For simple 2-line cases, a deterministic fast-path can bypass the LLM entirely.
