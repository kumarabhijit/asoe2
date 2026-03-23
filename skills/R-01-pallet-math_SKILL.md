---
name: r-01-pallet-math-engine
description: >
  Deterministic pallet arithmetic for ASOE broken layer/pallet validation. Use this recipe whenever a
  sales order line item quantity must be evaluated against Ti×Hi pallet configuration. Triggers: any
  inbound SO line needing layer alignment check, pallet fill calculation, remainder computation, or
  quantity-to-pallet decomposition. Inputs are order_qty, Ti, Hi from ZASOE_PALLET_CFG. Outputs are
  remainder_mod_ti, remainder_pct_of_layer, full_layers, full_pallets, leftover_cases, pallet_fill_pct,
  and status enum. Called by S-01 Orchestrator for every line item on every order. Pure math — no AI,
  no customer context, no business rules. This recipe is the foundation for R-03, R-04, R-05, and R-06.
---

# R-01 — Pallet Math Engine

## 1. Problem domain

Every SKU in a CPG warehouse has a pallet specification defined by Ti (cases per layer) and Hi (layers
per pallet). When a customer orders a quantity that does not align to these multiples, the order creates
fulfillment inefficiency. This recipe performs the core modular arithmetic to decompose any order quantity
into its pallet components and classify its alignment status.

This recipe is stateless and context-free — it knows nothing about customers, business rules, or pricing.
It answers exactly one question: given this quantity and this pallet spec, what is the structural breakdown?

## 2. Data source

| Table | Fields used | Lookup key |
|---|---|---|
| ZASOE_PALLET_CFG | TI, HI, FULL_LAYER_QTY, FULL_PALLET_QTY, CASE_WEIGHT_KG, MAX_PALLET_WEIGHT_KG | MATNR |

The recipe reads one row per invocation. The caller (S-01) is responsible for the lookup and passing
Ti/Hi as inputs — this recipe does not perform database access.

## 3. Inputs

```json
{
  "order_qty": 47,
  "ti": 10,
  "hi": 5,
  "case_weight_kg": 4.08,
  "max_pallet_weight_kg": 1000
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| order_qty | integer | yes | Customer order quantity in base UOM (CS) |
| ti | integer | yes | Cases per layer (from pallet config) |
| hi | integer | yes | Layers per pallet (from pallet config) |
| case_weight_kg | float | no | Weight per case — for weight limit check |
| max_pallet_weight_kg | float | no | Max pallet weight — for weight limit check |

## 4. Computation logic

```
full_pallet_qty       = ti × hi
full_layer_qty        = ti

remainder_mod_ti      = order_qty MOD ti
remainder_mod_pallet  = order_qty MOD full_pallet_qty

full_layers           = FLOOR(order_qty / ti)
partial_layer_cases   = remainder_mod_ti

full_pallets          = FLOOR(order_qty / full_pallet_qty)
leftover_after_pallets = order_qty - (full_pallets × full_pallet_qty)
leftover_layers       = FLOOR(leftover_after_pallets / ti)

remainder_pct_of_layer = (remainder_mod_ti / ti) × 100

pallet_fill_pct = IF full_pallets > 0 AND leftover_after_pallets > 0:
                    ROUND((leftover_after_pallets / full_pallet_qty) × 100)
                  ELSE IF full_pallets = 0:
                    ROUND((order_qty / full_pallet_qty) × 100)
                  ELSE:
                    100

weight_check = IF case_weight_kg AND max_pallet_weight_kg:
                 order_qty × case_weight_kg <= max_pallet_weight_kg × CEIL(order_qty / full_pallet_qty)
               ELSE: null (skip)
```

### Status classification

```
IF remainder_mod_ti = 0 AND remainder_mod_pallet = 0:
    status = "FULL_PALLET"
ELSE IF remainder_mod_ti = 0 AND remainder_mod_pallet > 0:
    status = "FULL_LAYER"
ELSE IF remainder_mod_ti > 0 AND remainder_mod_pallet = 0:
    status = "BROKEN_LAYER"       # edge case: can't happen mathematically
ELSE IF remainder_mod_ti > 0 AND full_pallets > 0:
    status = "BROKEN_PALLET_AND_LAYER"
ELSE:
    status = "BROKEN_LAYER"
```

## 5. Outputs

```json
{
  "order_qty": 47,
  "ti": 10,
  "hi": 5,
  "full_layer_qty": 10,
  "full_pallet_qty": 50,
  "remainder_mod_ti": 7,
  "remainder_pct_of_layer": 70.0,
  "full_layers": 4,
  "partial_layer_cases": 7,
  "remainder_mod_pallet": 47,
  "full_pallets": 0,
  "leftover_after_pallets": 47,
  "leftover_layers": 4,
  "pallet_fill_pct": 94,
  "weight_ok": true,
  "status": "BROKEN_LAYER"
}
```

| Field | Type | Description |
|---|---|---|
| remainder_mod_ti | integer | Cases that don't fill a complete layer |
| remainder_pct_of_layer | float | How full the partial layer is (0-99%) |
| full_layers | integer | Number of completely filled layers |
| partial_layer_cases | integer | Cases in the incomplete layer (= remainder_mod_ti) |
| full_pallets | integer | Number of completely filled pallets |
| leftover_after_pallets | integer | Cases remaining after full pallets extracted |
| leftover_layers | integer | Full layers within the leftover |
| pallet_fill_pct | integer | Fill efficiency of the last (incomplete) pallet |
| weight_ok | boolean/null | Whether qty respects weight limit; null if not checked |
| status | enum | FULL_PALLET, FULL_LAYER, BROKEN_LAYER, BROKEN_PALLET_AND_LAYER |

## 6. Edge cases

| Case | Input | Expected output |
|---|---|---|
| Qty = 0 | order_qty=0, ti=10, hi=5 | All zeros, status=FULL_PALLET (vacuously true) |
| Qty = 1 | order_qty=1, ti=10, hi=5 | remainder=1, pct=10%, status=BROKEN_LAYER |
| Qty = Ti | order_qty=10, ti=10, hi=5 | remainder=0, full_layers=1, status=FULL_LAYER |
| Qty = pallet | order_qty=50, ti=10, hi=5 | Everything aligned, status=FULL_PALLET |
| Qty > pallet | order_qty=115, ti=10, hi=5 | 2 full pallets + 15 leftover, status=BROKEN_PALLET_AND_LAYER |
| Qty exactly 2 pallets | order_qty=100, ti=10, hi=5 | status=FULL_PALLET |
| Non-standard Ti | order_qty=80, ti=8, hi=6 | pallet=48, 1 full pallet + 32 leftover (4 full layers), status=FULL_LAYER |
| Weight exceeded | order_qty=300, case_weight=7.8, max=1000 | weight_ok=false on per-pallet basis |

## 7. Test assertions (against mock data)

| Test | Order | Line | Expected status | Expected remainder |
|---|---|---|---|---|
| BLP-TEST-001 | 0000088421 | 000010 | FULL_PALLET | 0 |
| BLP-TEST-002 | 0000088430 | 000010 | BROKEN_LAYER | 7 |
| BLP-TEST-004 | 0000088423 | 000030 | FULL_LAYER | 0 (mod ti), 32 (mod pallet) |
| BLP-TEST-005 | 0000088432 | 000010 | BROKEN_PALLET_AND_LAYER | 5 (mod ti), 15 (mod pallet) |
| BLP-TEST-007 | 0000088434 | 000010 | FULL_LAYER | 0 |

## 8. Dependencies

- None. This is a leaf recipe with no upstream recipe dependencies.
- Called by: S-01 Orchestrator, R-03 Rule Matcher, R-04 Rounding Calculator, R-06 Cross-Line Detector.

## 9. Performance

Execution: < 1ms. Pure arithmetic, no I/O. Stateless — can be parallelized across all lines on an order.
