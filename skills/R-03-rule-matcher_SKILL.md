---
name: r-03-validation-rule-matcher
description: >
  Deterministic business rule evaluation for ASOE broken layer/pallet validation. Evaluates 8 prioritized
  rules from ZASOE_PALLET_RULES against the pallet math result (R-01) and customer policy (R-02). Returns
  the first matching rule ID and its prescribed action: PASS, PASS_WITH_SURCHARGE, AUTO_ROUND_UP,
  PROPOSE_ALTERNATIVES, REJECT_OR_SURCHARGE, OPTIMIZE_MIXED_PALLET, or REJECT_LINE. First-match-wins
  evaluation in priority order. Called per line item by S-01 Orchestrator. No AI — pure conditional
  evaluation against a rule table.
---

# R-03 — Validation Rule Matcher

## 1. Problem domain

After R-01 computes the pallet math and R-02 loads the customer policy, this recipe determines what
action to take. It evaluates the 8 business rules defined in ZASOE_PALLET_RULES in priority order and
returns the first rule that matches the current state. This is the core decision engine — every line
item on every order flows through this recipe exactly once.

The rule table is externalized (not hardcoded) so that business users can add, modify, or reorder rules
without code changes. Rules are evaluated strictly by priority — first match wins, remaining rules are
not evaluated.

## 2. Data source

| Table | Fields used | Lookup key |
|---|---|---|
| ZASOE_PALLET_RULES | RULE_ID, RULE_NAME, PRIORITY, CONDITION, ACTION | All rows, sorted by PRIORITY ASC |

## 3. Inputs

```json
{
  "pallet_math": {
    "order_qty": 47,
    "ti": 10,
    "hi": 5,
    "full_pallet_qty": 50,
    "remainder_mod_ti": 7,
    "remainder_pct_of_layer": 70.0,
    "full_pallets": 0,
    "remainder_mod_pallet": 47,
    "status": "BROKEN_LAYER"
  },
  "customer_policy": {
    "broken_layer_allowed": false,
    "broken_layer_surcharge_pct": 0,
    "auto_round_up_tolerance_pct": 5,
    "require_full_layer": true,
    "require_full_pallet": false,
    "mixed_sku_pallet_allowed": true
  },
  "round_up_delta_pct": 6.38,
  "has_multiple_broken_lines": false,
  "lines_share_mixable_family": false
}
```

The caller (S-01) pre-computes `round_up_delta_pct` using R-04, and `has_multiple_broken_lines` /
`lines_share_mixable_family` using a quick pre-scan of the order.

## 4. Rule evaluation logic

Rules are evaluated in this exact sequence. First match terminates evaluation.

### Rule BLP-001: Full layer — no action (Priority 10)
```
IF remainder_mod_ti = 0:
    RETURN { rule: "BLP-001", action: "PASS" }
```

### Rule BLP-002: Full pallet — no action (Priority 20)
```
IF remainder_mod_pallet = 0:
    RETURN { rule: "BLP-002", action: "PASS" }
```
Note: BLP-001 catches layer-aligned orders; BLP-002 catches the subset that are also pallet-aligned.
In practice, if remainder_mod_ti = 0, BLP-001 fires first regardless. BLP-002 exists for explicit
pallet-level logging.

### Rule BLP-003: Broken layer — customer waiver (Priority 30)
```
IF remainder_mod_ti != 0
   AND customer_policy.broken_layer_allowed = true:
    RETURN { rule: "BLP-003", action: "PASS_WITH_SURCHARGE",
             surcharge_pct: customer_policy.broken_layer_surcharge_pct }
```

### Rule BLP-004: Near-full layer — auto round up (Priority 40)
```
IF remainder_pct_of_layer >= 70
   AND customer_policy.auto_round_up_tolerance_pct > 0
   AND round_up_delta_pct <= customer_policy.auto_round_up_tolerance_pct:
    RETURN { rule: "BLP-004", action: "AUTO_ROUND_UP" }
```

### Rule BLP-005: Broken layer — propose alternatives (Priority 50)
```
IF remainder_pct_of_layer >= 30
   AND remainder_pct_of_layer < 70
   AND customer_policy.broken_layer_allowed = false:
    RETURN { rule: "BLP-005", action: "PROPOSE_ALTERNATIVES" }
```

### Rule BLP-006: Small remainder — surcharge or reject (Priority 60)
```
IF remainder_mod_ti > 0
   AND remainder_pct_of_layer < 30
   AND customer_policy.broken_layer_allowed = false:
    RETURN { rule: "BLP-006", action: "REJECT_OR_SURCHARGE" }
```

### Rule BLP-007: Cross-line mixed pallet opportunity (Priority 70)
```
IF has_multiple_broken_lines = true
   AND lines_share_mixable_family = true
   AND customer_policy.mixed_sku_pallet_allowed = true:
    RETURN { rule: "BLP-007", action: "OPTIMIZE_MIXED_PALLET" }
```
Note: This rule fires at the order level, not the line level. The orchestrator sets the
`has_multiple_broken_lines` flag after initial per-line processing.

### Rule BLP-008: Strict customer — reject line (Priority 80)
```
IF customer_policy.require_full_pallet = true
   AND remainder_mod_pallet != 0
   AND NOT within_auto_round_tolerance:
    RETURN { rule: "BLP-008", action: "REJECT_LINE" }
```

### Fallback (no rule matched)
```
RETURN { rule: "BLP-DEFAULT", action: "MANUAL_REVIEW",
         reason: "No rule matched — escalate to supervisor" }
```

## 5. Outputs

```json
{
  "matched_rule_id": "BLP-005",
  "matched_rule_name": "Broken layer — propose alternatives",
  "action": "PROPOSE_ALTERNATIVES",
  "priority": 50,
  "surcharge_pct": null,
  "evaluation_trace": [
    { "rule": "BLP-001", "result": "SKIP", "reason": "remainder_mod_ti = 7 (not 0)" },
    { "rule": "BLP-002", "result": "SKIP", "reason": "remainder_mod_pallet = 47 (not 0)" },
    { "rule": "BLP-003", "result": "SKIP", "reason": "broken_layer_allowed = false" },
    { "rule": "BLP-004", "result": "SKIP", "reason": "round_up_delta 6.38% > tolerance 5%" },
    { "rule": "BLP-005", "result": "MATCH", "reason": "remainder 70% in [30,70) range" }
  ]
}
```

The `evaluation_trace` is included for audit trail transparency — it shows exactly why each
skipped rule did not match, enabling debugging and rule tuning.

## 6. Edge cases

| Case | Behavior |
|---|---|
| Qty perfectly aligned | BLP-001 fires, evaluation stops. No downstream processing. |
| Customer allows broken layers AND remainder is small | BLP-003 fires (priority 30 beats BLP-006 priority 60). Surcharge applied. |
| Auto-round tolerance exceeded | BLP-004 skips, falls through to BLP-005 or BLP-006 depending on remainder size. |
| Cross-line eligible but customer forbids mixed pallets | BLP-007 skips (mixed_sku_pallet_allowed = false), falls through to BLP-008. |
| No rules match (shouldn't happen with current ruleset) | Returns MANUAL_REVIEW fallback with reason. |
| Rule table is empty | Returns MANUAL_REVIEW fallback. Log critical error. |

## 7. Test assertions

| Test | Order | Line | Expected rule | Expected action |
|---|---|---|---|---|
| BLP-TEST-001 | 0000088421 | 000010 | BLP-002 | PASS |
| BLP-TEST-002 | 0000088430 | 000010 | BLP-005 | PROPOSE_ALTERNATIVES |
| BLP-TEST-003 | 0000088431 | 000010 | BLP-003 | PASS_WITH_SURCHARGE |
| BLP-TEST-004 | 0000088423 | 000030 | BLP-008 | REJECT_LINE |
| BLP-TEST-005 | 0000088432 | 000010 | BLP-008 | REJECT_LINE |
| BLP-TEST-006 | 0000088433 | 000010 | BLP-003 | PASS_WITH_SURCHARGE |
| BLP-TEST-007 | 0000088434 | 000010 | BLP-001 | PASS |

## 8. Dependencies

- Upstream: R-01 (pallet math result), R-02 (customer policy), R-04 (round_up_delta_pct — pre-computed).
- Called by: S-01 Orchestrator (once per line item).
- Downstream: Triggers R-04 (for PROPOSE/ROUND actions), R-05 (for SURCHARGE actions), or S-02 (for OPTIMIZE).

## 9. Rule table maintenance

Rules are stored in ZASOE_PALLET_RULES and can be modified by authorized users. When adding rules:
- Assign a priority between existing rules (gaps of 10 allow insertion).
- Ensure no two rules have the same priority.
- Test the new rule against all 7 test scenarios before activating.
- The evaluation trace output makes it easy to verify rule ordering.
