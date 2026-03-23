---
name: r-04-rounding-calculator
description: >
  Deterministic quantity rounding for ASOE broken layer/pallet validation. Computes the nearest valid
  round-up and round-down quantities aligned to layer or pallet boundaries. Calculates qty delta, delta
  percentage, and dollar impact for each option using PR00 base price. Checks whether each option falls
  within the customer's auto-round tolerance. Called by S-01 when R-03 returns PROPOSE_ALTERNATIVES,
  AUTO_ROUND_UP, or REJECT_LINE (to provide nearest valid quantities). Pure math — no AI reasoning.
---

# R-04 — Quantity Rounding Calculator

## 1. Problem domain

When an order line has a broken layer or broken pallet, the business needs to know: what are the nearest
valid quantities, and what is the financial impact of adjusting? This recipe computes both directions
(up and down) at both granularities (layer and pallet), giving the orchestrator and the customer
communication skill concrete numbers to work with.

## 2. Data sources

| Table | Fields used | Lookup key |
|---|---|---|
| ZASOE_PALLET_CFG | TI, HI, FULL_PALLET_QTY | MATNR (via R-01 output) |
| KONP | KBETR (PR00 base price) | MATNR + KSCHL='PR00' |

## 3. Inputs

```json
{
  "order_qty": 47,
  "ti": 10,
  "full_pallet_qty": 50,
  "unit_price": 14.88,
  "auto_round_up_tolerance_pct": 5,
  "auto_round_down_tolerance_pct": 0,
  "require_full_pallet": false
}
```

## 4. Computation logic

### Layer-aligned rounding
```
round_up_layer   = CEIL(order_qty / ti) × ti
round_down_layer = FLOOR(order_qty / ti) × ti

IF round_down_layer = 0: round_down_layer = ti    # Never round to zero
```

### Pallet-aligned rounding
```
round_up_pallet   = CEIL(order_qty / full_pallet_qty) × full_pallet_qty
round_down_pallet = FLOOR(order_qty / full_pallet_qty) × full_pallet_qty

IF round_down_pallet = 0: round_down_pallet = full_pallet_qty    # Minimum 1 pallet
```

### Delta calculations (for each option)
```
delta_qty     = proposed_qty - order_qty
delta_pct     = ROUND((delta_qty / order_qty) × 100, 2)
delta_dollars = ROUND(delta_qty × unit_price, 2)
```

### Tolerance check
```
within_up_tolerance   = ABS(delta_pct_up)   <= auto_round_up_tolerance_pct
within_down_tolerance = ABS(delta_pct_down) <= auto_round_down_tolerance_pct
```

### Nearest valid quantities (for reject scenario)
```
IF require_full_pallet:
    nearest_valid = [round_down_pallet, round_up_pallet]
ELSE:
    nearest_valid = [round_down_layer, round_up_layer]

# Remove duplicates, sort ascending
nearest_valid = SORTED(UNIQUE(nearest_valid))
```

## 5. Outputs

```json
{
  "order_qty": 47,
  "options": {
    "round_up_layer": {
      "qty": 50,
      "delta_qty": 3,
      "delta_pct": 6.38,
      "delta_dollars": 44.64,
      "within_tolerance": false,
      "alignment": "PALLET"
    },
    "round_down_layer": {
      "qty": 40,
      "delta_qty": -7,
      "delta_pct": -14.89,
      "delta_dollars": -104.16,
      "within_tolerance": false,
      "alignment": "LAYER"
    },
    "round_up_pallet": {
      "qty": 50,
      "delta_qty": 3,
      "delta_pct": 6.38,
      "delta_dollars": 44.64,
      "within_tolerance": false,
      "alignment": "PALLET"
    },
    "round_down_pallet": {
      "qty": 50,
      "delta_qty": 3,
      "delta_pct": 6.38,
      "delta_dollars": 44.64,
      "within_tolerance": false,
      "alignment": "PALLET"
    }
  },
  "nearest_valid_quantities": [40, 50],
  "best_round_up": { "qty": 50, "delta_dollars": 44.64 },
  "best_round_down": { "qty": 40, "delta_dollars": -104.16 },
  "any_within_tolerance": false
}
```

## 6. Edge cases

| Case | Behavior |
|---|---|
| Qty already aligned | Both round-up and round-down equal order_qty. Delta = 0. |
| Qty < Ti | round_down_layer would be 0 → floor to Ti instead. |
| Qty between 1 and 2 pallets | round_down_pallet = 1 pallet, round_up_pallet = 2 pallets. |
| Tolerance = 0 | within_tolerance always false — no auto-rounding permitted. |
| Unit price = 0 or missing | delta_dollars = 0. Log warning — pricing data missing. |
| round_up_layer = round_up_pallet | Deduplicate in nearest_valid_quantities. |

## 7. Dependencies

- Upstream: R-01 (for Ti, Hi, full_pallet_qty), R-02 (for tolerance percentages), KONP (for unit price).
- Called by: S-01 Orchestrator, S-03 Customer Communication Composer.
- Also used as pre-computation input for R-03 (round_up_delta_pct).

## 8. Performance

Execution: < 1ms. Pure arithmetic. No I/O if unit_price is passed in (which it should be — the
orchestrator loads pricing once and passes it down).
