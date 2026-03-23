---
name: r-06-cross-line-detector
description: >
  Deterministic cross-line mixed pallet eligibility detection for ASOE. Scans all line items on a single
  sales order to identify groups of broken lines that share the same MIXABLE_FAMILY and have compatible
  case dimensions. Checks customer policy for mixed-SKU pallet permission. Outputs candidate line groups
  for optimization by S-02. Does NOT decide how to combine — only whether combination is structurally
  possible. Called by S-01 after all per-line validation is complete. No AI — pure filtering and grouping.
---

# R-06 — Cross-Line Detector

## 1. Problem domain

ERPs validate each order line independently. This means two broken lines — say 23 cases of Cola and 17
cases of Diet Cola — are each treated as separate fulfillment problems, resulting in two partial pallets
shipped. But if both SKUs belong to the same product family and have identical case dimensions, they
could be combined onto a single mixed pallet with zero waste.

This recipe identifies those opportunities by grouping broken lines that share a MIXABLE_FAMILY, have
compatible physical dimensions, and belong to a customer that permits mixed-SKU pallets.

This recipe only detects eligibility — it does not solve the optimization problem. The actual pallet
build plan is computed by S-02 (Mixed Pallet Optimizer), which requires AI reasoning for complex
multi-line scenarios.

## 2. Data sources

| Table | Fields used | Lookup key |
|---|---|---|
| ZASOE_PALLET_CFG | MIXABLE_FAMILY, CASE_LENGTH_MM, CASE_WIDTH_MM, CASE_HEIGHT_MM, TI | MATNR |
| ZASOE_CUST_PALLET_EX | MIXED_SKU_PALLET_ALLOWED, MIXED_FAMILY_ONLY | KUNNR (via R-02) |

## 3. Inputs

```json
{
  "order_lines": [
    {
      "line_id": "000010",
      "matnr": "000000000000050042",
      "pallet_math": { "status": "BROKEN_LAYER", "remainder_mod_ti": 3, "order_qty": 23 },
      "pallet_config": {
        "ti": 10, "mixable_family": "BEV-CSD-COLA",
        "case_length_mm": 400, "case_width_mm": 267, "case_height_mm": 178
      }
    },
    {
      "line_id": "000020",
      "matnr": "000000000000050043",
      "pallet_math": { "status": "BROKEN_LAYER", "remainder_mod_ti": 7, "order_qty": 17 },
      "pallet_config": {
        "ti": 10, "mixable_family": "BEV-CSD-COLA",
        "case_length_mm": 400, "case_width_mm": 267, "case_height_mm": 178
      }
    }
  ],
  "customer_policy": {
    "mixed_sku_pallet_allowed": true,
    "mixed_family_only": false
  }
}
```

## 4. Detection logic

### Step 1 — Filter to broken lines only
```
broken_lines = order_lines.filter(line =>
    line.pallet_math.status IN ("BROKEN_LAYER", "BROKEN_PALLET_AND_LAYER")
)

IF broken_lines.length < 2:
    RETURN { eligible: false, reason: "Fewer than 2 broken lines on this order" }
```

### Step 2 — Check customer permits mixed pallets
```
IF customer_policy.mixed_sku_pallet_allowed = false:
    RETURN { eligible: false, reason: "Customer does not allow mixed-SKU pallets" }
```

### Step 3 — Group by MIXABLE_FAMILY
```
groups = GROUP broken_lines BY pallet_config.mixable_family

IF customer_policy.mixed_family_only = true:
    # Only groups with 2+ lines from the SAME family are candidates
    candidate_groups = groups.filter(g => g.lines.length >= 2)
ELSE:
    # Cross-family mixing allowed — all broken lines form one candidate group
    candidate_groups = [{ family: "CROSS_FAMILY", lines: broken_lines }]
```

### Step 4 — Validate case dimension compatibility within each group
```
FOR each group IN candidate_groups:
    base_dims = group.lines[0].pallet_config
    compatible = group.lines.ALL(line =>
        line.pallet_config.case_length_mm = base_dims.case_length_mm
        AND line.pallet_config.case_width_mm = base_dims.case_width_mm
        AND line.pallet_config.case_height_mm = base_dims.case_height_mm
    )
    IF NOT compatible:
        # Check if dimensions are within 5% tolerance (different pack sizes may vary slightly)
        compatible = group.lines.ALL(line =>
            ABS(line.case_length_mm - base_dims.case_length_mm) / base_dims.case_length_mm <= 0.05
            AND ABS(line.case_width_mm - base_dims.case_width_mm) / base_dims.case_width_mm <= 0.05
            AND ABS(line.case_height_mm - base_dims.case_height_mm) / base_dims.case_height_mm <= 0.05
        )
        IF compatible: group.dimension_match = "TOLERANCE_5PCT"
        ELSE: group.dimension_match = "INCOMPATIBLE"; REMOVE group from candidates

    group.dimension_match = group.dimension_match OR "EXACT"
```

### Step 5 — Compute combined metrics per group
```
FOR each group IN candidate_groups:
    group.combined_qty = SUM(line.order_qty for line in group.lines)
    group.combined_ti = group.lines[0].pallet_config.ti   # Use base Ti for combined calc
    group.combined_mod_ti = group.combined_qty MOD group.combined_ti
    group.combined_full_layers = FLOOR(group.combined_qty / group.combined_ti)
```

## 5. Outputs

```json
{
  "eligible": true,
  "candidate_groups": [
    {
      "group_id": "GRP-001",
      "family": "BEV-CSD-COLA",
      "lines": ["000010", "000020"],
      "combined_qty": 40,
      "combined_ti": 10,
      "combined_mod_ti": 0,
      "combined_full_layers": 4,
      "dimension_match": "EXACT",
      "line_details": [
        { "line_id": "000010", "matnr": "000000000000050042", "qty": 23, "remainder": 3 },
        { "line_id": "000020", "matnr": "000000000000050043", "qty": 17, "remainder": 7 }
      ]
    }
  ],
  "total_candidate_lines": 2,
  "reason": null
}
```

Ineligible output:
```json
{
  "eligible": false,
  "candidate_groups": [],
  "total_candidate_lines": 0,
  "reason": "Customer does not allow mixed-SKU pallets"
}
```

## 6. Edge cases

| Case | Behavior |
|---|---|
| Only 1 broken line | Ineligible — need at least 2 for cross-line optimization. |
| Customer forbids mixed pallets | Ineligible — even if families match. |
| Same family but different case dimensions | Check 5% tolerance. If outside, mark INCOMPATIBLE and exclude. |
| Mixed-family-only = false | All broken lines regardless of family form one candidate group. |
| 4+ broken lines, 2 families | Create 2 candidate groups (one per family) if mixed_family_only = true. |
| Combined qty still doesn't fill a layer | Still eligible — S-02 will evaluate whether the combo improves fill rate. |
| Lines with Ti = 8 and Ti = 10 mixed | Use the common Ti if case dims match; otherwise separate groups. |

## 7. Test assertions

| Test | Order | Expected eligible | Expected groups | Reason if ineligible |
|---|---|---|---|---|
| Kroger cross-line | 0000088433 | true | 1 group (BEV-CSD-COLA) | — |
| Target multi-line | 0000088423 | false | 0 | Mixed-SKU not allowed |
| Walmart clean | 0000088421 | false | 0 | Fewer than 2 broken lines |
| Costco | 0000088432 | false | 0 | Mixed-SKU not allowed |

## 8. Dependencies

- Upstream: R-01 (pallet math per line — to know which lines are broken), R-02 (customer policy).
- Called by: S-01 Orchestrator (once per order, after all per-line R-03 evaluations complete).
- Downstream: S-02 Mixed Pallet Optimizer (receives candidate_groups as input).

## 9. Performance

Execution: < 2ms. Linear scan over order lines, grouping, and dimension comparison. No I/O if pallet
configs are pre-loaded (which they should be — the orchestrator loads them during per-line processing).
