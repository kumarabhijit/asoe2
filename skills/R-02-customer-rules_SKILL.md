---
name: r-02-customer-rule-resolver
description: >
  Deterministic customer pallet policy lookup for ASOE broken layer/pallet validation. Use this recipe
  to retrieve a customer's pallet exception rules from ZASOE_CUST_PALLET_EX given a customer number and
  order date. Returns the full policy object: broken layer/pallet allowed flags, surcharge percentages,
  mixed-SKU pallet permissions, auto-round tolerances, and full layer/pallet requirements. Called once
  per order by S-01 Orchestrator before per-line processing begins. Pure lookup with date-range
  filtering — no AI reasoning, no interpretation. Downstream consumers: R-03, R-04, R-05, R-06.
---

# R-02 — Customer Rule Resolver

## 1. Problem domain

Every customer has a unique pallet policy negotiated as part of their trading agreement. Some accept
broken layers with a surcharge (Kroger). Some require full pallets with zero tolerance (Costco). Some
allow auto-rounding within a percentage band (Walmart). This recipe loads the active policy for a given
customer and date, providing the configuration context that all downstream recipes need.

This recipe is called once per order — not per line. The customer policy applies uniformly to all lines
on the same order.

## 2. Data source

| Table | Fields used | Lookup key |
|---|---|---|
| ZASOE_CUST_PALLET_EX | All fields | KUNNR + date within EFFECTIVE_FROM / EFFECTIVE_TO |

### Lookup logic

```
SELECT * FROM ZASOE_CUST_PALLET_EX
WHERE KUNNR = :customer_number
  AND EFFECTIVE_FROM <= :order_date
  AND EFFECTIVE_TO >= :order_date
ORDER BY EFFECTIVE_FROM DESC
LIMIT 1
```

If no active record is found, return a strict default policy (all flags = false, all tolerances = 0,
require full layer = true, require full pallet = true). This is the safest fallback — no silent
permissiveness.

## 3. Inputs

```json
{
  "kunnr": "0000100001",
  "order_date": "2026-03-22"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| kunnr | string | yes | SAP customer number (10-digit, zero-padded) |
| order_date | date | yes | Sales order date (AUDAT) for validity check |

## 4. Outputs

```json
{
  "kunnr": "0000100001",
  "customer_name": "Walmart Inc.",
  "policy_found": true,
  "effective_from": "2025-01-01",
  "effective_to": "2026-12-31",
  "broken_layer_allowed": false,
  "broken_layer_surcharge_pct": 0,
  "broken_pallet_allowed": true,
  "broken_pallet_surcharge_pct": 0,
  "mixed_sku_pallet_allowed": true,
  "mixed_family_only": true,
  "auto_round_up_tolerance_pct": 5,
  "auto_round_down_tolerance_pct": 0,
  "require_full_layer": true,
  "require_full_pallet": false,
  "notes": "Walmart requires full layer minimum. Mixed pallets OK within same product family."
}
```

| Field | Type | Description |
|---|---|---|
| policy_found | boolean | Whether an active exception record exists |
| broken_layer_allowed | boolean | Customer accepts partial layers without rejection |
| broken_layer_surcharge_pct | float | Surcharge % applied to broken layer cases (0 = none) |
| broken_pallet_allowed | boolean | Customer accepts non-full-pallet shipments |
| broken_pallet_surcharge_pct | float | Surcharge % on broken pallet orders |
| mixed_sku_pallet_allowed | boolean | Customer accepts multiple SKUs on one pallet |
| mixed_family_only | boolean | If mixed allowed, restrict to same MIXABLE_FAMILY |
| auto_round_up_tolerance_pct | float | Max % qty increase agent can apply without approval |
| auto_round_down_tolerance_pct | float | Max % qty decrease agent can apply without approval |
| require_full_layer | boolean | Reject if qty does not align to layer multiple |
| require_full_pallet | boolean | Reject if qty does not align to pallet multiple |

## 5. Default policy (no record found)

```json
{
  "policy_found": false,
  "broken_layer_allowed": false,
  "broken_layer_surcharge_pct": 0,
  "broken_pallet_allowed": false,
  "broken_pallet_surcharge_pct": 0,
  "mixed_sku_pallet_allowed": false,
  "mixed_family_only": false,
  "auto_round_up_tolerance_pct": 0,
  "auto_round_down_tolerance_pct": 0,
  "require_full_layer": true,
  "require_full_pallet": true,
  "notes": "DEFAULT — No customer exception on file. Strict full-pallet policy applied."
}
```

## 6. Edge cases

| Case | Behavior |
|---|---|
| No active record for customer | Return default strict policy. Log warning. |
| Multiple overlapping date ranges | Take most recent EFFECTIVE_FROM (newest agreement wins). |
| Policy expired (EFFECTIVE_TO < order_date) | Treat as no active record — return default. Log expiry warning. |
| Customer number not found in KNA1 | Return error — this is a master data issue, not a pallet issue. |
| Surcharge % > 0 but broken_layer_allowed = false | Valid configuration — surcharge applies only if a downstream rule overrides the rejection (e.g., manual exception). |

## 7. Test assertions

| Test | Customer | Expected policy_found | Expected require_full_pallet |
|---|---|---|---|
| Walmart | 0000100001 | true | false |
| Kroger | 0000100002 | true | false |
| Target | 0000100003 | true | true |
| Costco | 0000100004 | true | true |

## 8. Dependencies

- Upstream: None (leaf recipe — reads directly from master data).
- Called by: S-01 Orchestrator (once per order).
- Downstream consumers: R-03 Rule Matcher, R-04 Rounding Calculator, R-05 Surcharge Calculator, R-06 Cross-Line Detector.

## 9. Performance

Execution: Single indexed database read. < 5ms including network. Cacheable per customer for the
duration of order processing (policy does not change mid-order).
