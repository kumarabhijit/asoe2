---
name: s-01-pallet-validation-orchestrator
description: >
  Master orchestration skill for ASOE broken layer/pallet validation. This is the single entry point —
  all other recipes (R-01 through R-06) and skills (S-02, S-03) are invoked by this orchestrator. Use
  whenever an inbound sales order (VBAK/VBAP) needs pallet compliance validation. Triggers: new SO
  created, EDI 850 received, order modification (VA02), or manual re-validation request. The orchestrator
  loads customer policy once, runs per-line pallet math and rule matching, decides execution sequencing,
  triggers cross-line optimization when eligible, invokes customer communication when needed, and
  synthesizes the final order disposition with audit trail. AI reasoning required for sequencing
  decisions, edge case judgment, and narrative synthesis.
---

# S-01 — Pallet Validation Orchestrator

## 1. Problem domain

This skill is the brain of the broken layer/pallet validation workflow. It receives a complete sales
order, coordinates the execution of all deterministic recipes, decides when AI skills need to be
invoked, and produces a final order-level disposition (PASS / HOLD / PARTIAL_HOLD) with per-line
detail and an audit trail.

### Why this is a skill, not a recipe

The orchestration sequence is not always linear. The agent must make contextual decisions:
- Should cross-line optimization (R-06 → S-02) run before or after individual line resolutions?
  If run first and it consolidates two broken lines into a clean mixed pallet, the per-line surcharges
  become unnecessary — saving the customer money and reducing invoice complexity.
- When multiple lines on the same order have different actions (line 1 = PASS, line 2 = REJECT,
  line 3 = PROPOSE_ALTERNATIVES), the agent must synthesize an order-level disposition and decide
  whether to hold the entire order or release clean lines while holding problematic ones.
- The audit trail narrative requires natural language synthesis — explaining why each decision was
  made in a way that a CSR, supervisor, or auditor can understand without reading raw rule traces.

## 2. Trigger conditions

| Trigger | Source | Priority |
|---|---|---|
| New sales order created | SAP VA01 / EDI 850 inbound | Immediate |
| Order modification | SAP VA02 (qty change on existing line) | Immediate |
| Manual re-validation | CSR clicks "Re-validate" in ASOE UI | On-demand |
| Batch validation | Scheduled job for orders in HOLD status | Low (batch window) |

## 3. Inputs

```json
{
  "order": {
    "vbeln": "0000088430",
    "auart": "ZOR",
    "vkorg": "1000",
    "vtweg": "10",
    "spart": "01",
    "kunnr": "0000100001",
    "bstnk": "WM-PO-2026-44825",
    "audat": "2026-03-22",
    "items": [
      {
        "posnr": "000010",
        "matnr": "000000000000050042",
        "kwmeng": 47,
        "vrkme": "CS",
        "netpr": 14.88
      },
      {
        "posnr": "000020",
        "matnr": "000000000000050051",
        "kwmeng": 50,
        "vrkme": "CS",
        "netpr": 14.88
      }
    ]
  }
}
```

## 4. Execution workflow

### Phase 1 — Setup (deterministic)
```
1. Load customer policy via R-02(kunnr, audat)
2. Load pallet config for each unique MATNR on the order (ZASOE_PALLET_CFG lookup)
3. Load base prices for each MATNR (KONP PR00 lookup)
```

### Phase 2 — Per-line validation (deterministic, parallelizable)
```
FOR EACH line_item IN order.items:
    4. Run R-01(order_qty=kwmeng, ti, hi) → pallet_math
    5. Pre-compute round_up_delta_pct via R-04 (needed by R-03)
    6. Run R-03(pallet_math, customer_policy, round_up_delta_pct) → matched_rule, action
    7. IF action = "PASS":
           line_result = { action: "PASS", rule: matched_rule }
    8. IF action = "PASS_WITH_SURCHARGE":
           surcharge = R-05(broken_cases, unit_price, surcharge_pct)
           line_result = { action: "PASS_WITH_SURCHARGE", surcharge }
    9. IF action = "AUTO_ROUND_UP":
           rounding = R-04(order_qty, ti, full_pallet_qty, unit_price, tolerances)
           line_result = { action: "AUTO_ROUND_UP", new_qty: rounding.best_round_up.qty }
    10. IF action = "PROPOSE_ALTERNATIVES":
           rounding = R-04(...)
           line_result = { action: "PROPOSE_ALTERNATIVES", options: rounding.options }
    11. IF action = "REJECT_LINE" OR "REJECT_OR_SURCHARGE":
           rounding = R-04(...)
           line_result = { action: "REJECT_LINE", nearest_valid: rounding.nearest_valid_quantities }
```

### Phase 3 — Cross-line optimization (AI decision point)
```
12. Collect all line results
13. Count broken lines: broken_count = lines WHERE action NOT IN ("PASS")
14. IF broken_count >= 2:
        cross_line = R-06(order_lines_with_pallet_math, customer_policy)
        IF cross_line.eligible:
            # AI DECISION: Should we optimize before finalizing per-line actions?
            # If optimization eliminates broken layers, surcharges become unnecessary.
            optimization = S-02(cross_line.candidate_groups, pallet_configs, customer_policy)
            IF optimization.recommended AND optimization.savings.total > threshold:
                # Override per-line results for optimized lines
                UPDATE line_results with optimization outcome
```

### Phase 4 — Communication (AI skill)
```
15. FOR EACH line WHERE action requires customer contact:
        IF action IN ("PROPOSE_ALTERNATIVES", "REJECT_LINE"):
            communication = S-03(action, rounding_options, customer_info, order_context)
            ATTACH communication.email_draft to line_result
            ATTACH communication.csr_notes to line_result
```

### Phase 5 — Order disposition (AI synthesis)
```
16. Determine order-level status:
    IF ALL lines action = "PASS":
        order_disposition = "PASS"
    ELSE IF ALL lines action IN ("PASS", "PASS_WITH_SURCHARGE", "AUTO_ROUND_UP"):
        order_disposition = "PASS" (with modifications)
    ELSE IF ANY line action = "REJECT_LINE":
        # AI DECISION: Hold entire order or release clean lines?
        # Consider: customer preference, shipping efficiency, urgency
        order_disposition = "PARTIAL_HOLD" or "HOLD"
    ELSE:
        order_disposition = "HOLD"

17. Generate audit trail narrative (AI):
    Synthesize human-readable explanation of all decisions, rule matches,
    and actions taken. Include dollar impact summary.
```

## 5. Outputs

```json
{
  "order_id": "0000088430",
  "customer": "Walmart Inc.",
  "disposition": "PARTIAL_HOLD",
  "summary": "2 lines evaluated. Line 10: broken layer (7 of 10), proposed round-up/down alternatives — awaiting customer response. Line 20: full pallet, released.",
  "lines": [
    {
      "posnr": "000010",
      "action": "PROPOSE_ALTERNATIVES",
      "rule_matched": "BLP-005",
      "pallet_math": { "remainder": 7, "status": "BROKEN_LAYER" },
      "options": {
        "round_up": { "qty": 50, "delta_dollars": 44.64 },
        "round_down": { "qty": 40, "delta_dollars": -104.16 }
      },
      "communication": {
        "email_draft_id": "COMM-88430-010",
        "status": "PENDING_CSR_REVIEW"
      }
    },
    {
      "posnr": "000020",
      "action": "PASS",
      "rule_matched": "BLP-002",
      "pallet_math": { "remainder": 0, "status": "FULL_PALLET" }
    }
  ],
  "cross_line_optimization": null,
  "total_at_risk_dollars": 104.16,
  "audit_trail": {
    "timestamp": "2026-03-22T14:32:07Z",
    "agent": "ASOE-S01-v1.0",
    "customer_policy_loaded": "ZASOE_CUST_PALLET_EX effective 2025-01-01",
    "evaluation_duration_ms": 247,
    "rules_evaluated": 5,
    "narrative": "Order WM-PO-2026-44825 from Walmart evaluated against pallet compliance rules. Line 10 (SKU-0042, 47 CS) has a broken layer — 7 cases fill 70% of layer 5. Auto-round-up was considered but the 6.38% increase exceeds Walmart's 5% tolerance. Two alternatives generated: round up to 50 ($44.64 additional) or round down to 40 ($104.16 reduction). Routed to CSR for customer communication. Line 20 (SKU-0051, 50 CS) is a full pallet — passed with no action."
  }
}
```

## 6. AI reasoning scenarios

### Scenario A — Sequencing decision
Three broken lines on a Kroger order. Lines 1+2 share a family (can be mixed), line 3 is different.
The orchestrator must decide: run cross-line first (potentially eliminating surcharges on lines 1+2),
then process line 3 separately. Or process all three individually first, then check if cross-line
improves the outcome. The AI evaluates which sequence minimizes total cost and handling.

### Scenario B — Partial hold vs. full hold
Target order with 4 lines: 2 pass, 2 rejected. Shipping the 2 clean lines immediately saves time and
reduces OTIF risk. But Target prefers complete shipments — splitting creates a partial delivery that
may trigger a compliance penalty. The agent weighs these trade-offs using customer preference data.

### Scenario C — Threshold judgment for optimization
Cross-line optimization saves $11.90 on a $595 order (2%). Is that worth the complexity of a mixed
pallet? The agent considers the warehouse's mixed-pallet handling cost (typically $5-8 per occurrence)
and only recommends optimization when net savings exceed the handling overhead.

## 7. Dependencies

| Component | Type | Invocation |
|---|---|---|
| R-01 Pallet Math | Recipe | Per line item |
| R-02 Customer Rules | Recipe | Once per order (setup) |
| R-03 Rule Matcher | Recipe | Per line item |
| R-04 Rounding Calc | Recipe | Per line needing rounding |
| R-05 Surcharge Calc | Recipe | Per line with surcharge action |
| R-06 Cross-Line Detect | Recipe | Once per order (after per-line phase) |
| S-02 Mixed Pallet Optimizer | Skill | Per eligible candidate group |
| S-03 Customer Comms | Skill | Per line needing customer contact |

## 8. Error handling

| Error | Behavior |
|---|---|
| Pallet config not found for MATNR | Skip validation for that line. Flag as "CONFIG_MISSING". |
| Customer policy expired | Use default strict policy. Include warning in audit trail. |
| R-03 returns no match | Default to MANUAL_REVIEW. Escalate to supervisor queue. |
| S-02 or S-03 fails | Fall back to per-line results without optimization/communication. Log error. |
| Order has > 50 line items | Process in batches of 20. Log performance warning. |

## 9. Metrics and observability

| Metric | Description |
|---|---|
| orders_processed | Total orders evaluated |
| lines_per_action | Count by action type (PASS, SURCHARGE, REJECT, etc.) |
| cross_line_optimizations | Count of orders where cross-line improved outcome |
| avg_evaluation_duration_ms | End-to-end processing time per order |
| customer_comms_generated | Emails drafted by S-03 |
| auto_round_applied | Lines where qty was auto-adjusted |
| escalation_rate | % of lines falling to MANUAL_REVIEW |
