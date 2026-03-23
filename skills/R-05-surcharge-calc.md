---
name: r-05-surcharge-calculator
description: >
  Deterministic broken layer/pallet surcharge calculation for ASOE. Computes the ZBLS condition record
  amount when a customer's policy permits broken layers with a surcharge. Inputs are the number of
  broken cases, unit price, and surcharge percentage from R-02. Outputs the dollar amount and a SAP
  condition record payload ready for VA01/VA02 posting. Called by S-01 when R-03 returns
  PASS_WITH_SURCHARGE. Pure arithmetic — no AI, no judgment.
---

# R-05 — Surcharge Calculator

## 1. Problem domain

Some customers (e.g., Kroger) negotiate contracts that allow broken layer shipments but impose a
percentage surcharge on the non-aligned cases. This recipe calculates the exact surcharge amount and
formats it as a SAP pricing condition record (ZBLS) that can be attached to the sales order line item.

The surcharge applies only to the broken cases — not the full line quantity. If a customer orders 23
cases and Ti=10, only the 3 cases in the partial layer are surcharged, not all 23.

## 2. Data sources

| Table | Fields used | Lookup key |
|---|---|---|
| KONP | KSCHL='ZBLS', KBETR (surcharge rate) | KUNNR |
| ZASOE_CUST_PALLET_EX | BROKEN_LAYER_SURCHARGE_PCT | KUNNR (via R-02) |

## 3. Inputs

```json
{
  "order_id": "0000088431",
  "line_id": "000010",
  "matnr": "000000000000050042",
  "order_qty": 23,
  "broken_cases": 3,
  "unit_price": 14.88,
  "surcharge_pct": 8.0,
  "surcharge_scope": "broken_layer_cases_only"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| broken_cases | integer | yes | Number of cases in the partial layer (from R-01 remainder_mod_ti) |
| unit_price | float | yes | PR00 base price per case |
| surcharge_pct | float | yes | Customer's broken layer surcharge % (from R-02) |
| surcharge_scope | enum | no | "broken_layer_cases_only" (default) or "full_line_qty" |

## 4. Computation logic

```
IF surcharge_scope = "broken_layer_cases_only":
    surcharge_base = broken_cases × unit_price
ELSE IF surcharge_scope = "full_line_qty":
    surcharge_base = order_qty × unit_price

surcharge_amount = ROUND(surcharge_base × (surcharge_pct / 100), 2)
surcharge_per_case = ROUND(surcharge_amount / order_qty, 4)
```

### SAP condition record payload
```json
{
  "KSCHL": "ZBLS",
  "KBETR": surcharge_amount,
  "KONWA": "USD",
  "KPEIN": 1,
  "KMEIN": "CS",
  "KRECH": "A",
  "KWERT": surcharge_amount,
  "KOEIN": "USD",
  "LIFNR": "",
  "MATNR": matnr,
  "KUNNR": kunnr,
  "VBELN": order_id,
  "POSNR": line_id,
  "MWSK1": "",
  "KTEXT": "Broken layer surcharge — {broken_cases} cases at {surcharge_pct}%"
}
```

## 5. Outputs

```json
{
  "surcharge_amount": 3.57,
  "surcharge_per_case": 0.1552,
  "surcharge_base_value": 44.64,
  "surcharge_scope": "broken_layer_cases_only",
  "broken_cases_surcharged": 3,
  "condition_record": {
    "KSCHL": "ZBLS",
    "KBETR": 3.57,
    "KONWA": "USD",
    "KPEIN": 1,
    "KMEIN": "CS",
    "KWERT": 3.57,
    "KTEXT": "Broken layer surcharge — 3 cases at 8.0%"
  },
  "audit": {
    "calculation": "3 cases × $14.88 × 8.0% = $3.57",
    "customer_agreement": "ZASOE_CUST_PALLET_EX effective 2025-03-01 to 2026-12-31"
  }
}
```

## 6. Edge cases

| Case | Behavior |
|---|---|
| Surcharge pct = 0 | Return surcharge_amount = 0. Still generate condition record (zero-value ZBLS for audit). |
| Broken cases = 0 | Should not be called — caller error. Return surcharge_amount = 0 with warning. |
| Full line qty scope | Multiply surcharge against entire line value, not just broken cases. |
| Unit price missing | Return error — cannot calculate without base price. |
| Rounding to 2 decimals creates penny discrepancy | Round to nearest cent. Document rounding method in audit. |

## 7. Test assertions

| Test | Order | Line | Broken cases | Surcharge % | Expected amount |
|---|---|---|---|---|---|
| Kroger L1 | 0000088431 | 000010 | 3 | 8% | $3.57 |
| Kroger L1 (cross-line) | 0000088433 | 000010 | 3 | 8% | $3.57 |
| Kroger L2 (cross-line) | 0000088433 | 000020 | 7 | 8% | $8.33 |

## 8. Dependencies

- Upstream: R-01 (broken_cases count), R-02 (surcharge_pct), KONP/PR00 (unit_price).
- Called by: S-01 Orchestrator when action = PASS_WITH_SURCHARGE.
- Downstream: Condition record payload is passed to SAP VA02 posting function.

## 9. Performance

Execution: < 1ms. Single multiplication and rounding.
