# Skill: Duplicate PO Exception Management

## Description

Post-commit validation of saved orders flagged as potential duplicates. Fires after the order has been written to the ERP — whether it arrived via EDI 850 or was keyed manually by a human agent. Classifies duplicate probability using multi-signal scoring, enforces compliance gating, and routes to hold, review, annotation, or release. Trigger on event_type `EDI_850_DUPLICATE_PO` or `MANUAL_ENTRY_DUPLICATE_PO`.

---

## 1. Overview

This skill is a **second-line backstop** for duplicate PO detection. It runs **after** the order has been saved in the ERP system (SAP, Oracle, etc.) or ingested from an EDI 850 transaction. By the time this skill fires, the order already exists in the order book.

### 1.1 Why This Skill Exists

ERP systems like SAP do perform basic duplicate PO checks during order entry (e.g., SAP's built-in warning in VA01/VA02/ME21N). However, these checks are limited — typically a PO number string match against the same sold-to party — and they are **advisory, not enforced**. Agents routinely override them under time pressure. EDI-originated orders bypass manual entry entirely, so the ERP warning never fires.

This skill compensates for that gap by running a **multi-signal composite score** after the order is committed, catching duplicates that the ERP either missed or warned about but did not block.

### 1.2 Operational Model

Every action this skill produces is **retroactive**. The order is already saved. The skill cannot prevent entry — it can only:

- **Hold** an existing order (pause fulfillment pending resolution)
- **Annotate** an existing order (flag it for visibility but allow fulfillment)
- **Clear** an existing order (confirm it is not a duplicate)
- **Escalate** to a human reviewer with a pre-assembled context bundle

The language throughout this document reflects this post-commit reality. There is no "blocking" — only holding and releasing.

---

## 2. Trigger Conditions

This skill activates when **all** of the following are true:

- `event.event_type` is one of:
  - `EDI_850_DUPLICATE_PO` — order entered via EDI 850 processing
  - `MANUAL_ENTRY_DUPLICATE_PO` — order keyed by a human agent in the ERP
- `event.metadata.signal_scores` is present and non-empty
- `event.metadata.incoming_po_number` is present
- `event.metadata.customer_id` is present
- `event.metadata.original_po_ref` is present (the existing PO being matched against)
- `event.metadata.trading_partner_id` is present
- `event.metadata.source_channel` is present and one of `EDI` or `MANUAL`

If any required field is missing or malformed, do **not** proceed. Emit `PAYLOAD_VALIDATION_FAILURE` and route to `FAIL_TO_HUMAN` with a description of which fields are missing.

### 2.1 Optional Trigger Fields

| Field                                  | Channel    | Purpose                                                     |
|----------------------------------------|------------|-------------------------------------------------------------|
| `event.metadata.prior_rejection_ref`   | Both       | Present if this PO is a resubmission of a previously held/rejected order. Pass through to the recipe. |
| `event.metadata.erp_warning_issued`    | MANUAL     | `true` if the ERP issued a duplicate warning during entry that the agent overrode. Absent or `false` for EDI. |
| `event.metadata.entering_agent_id`     | MANUAL     | ID of the human agent who entered the order. Used for routing notifications back to the responsible party. |
| `event.metadata.erp_order_number`      | Both       | The ERP-assigned order number (e.g., SAP sales order number). May differ from the PO number. |

---

## 3. Signal Score Contract

The `event.metadata.signal_scores` object must contain per-signal match scores. Each value is a float in **[0.0, 1.0]**.

> **These keys must match `DuplicatePORecipe.SIGNAL_KEYS` exactly.** If the recipe's signal list changes, this section must be updated.

| Signal Key        | Weight | What It Measures                                                 |
|-------------------|--------|------------------------------------------------------------------|
| `po_number`       | 0.30   | Exact or fuzzy match of the PO number string                    |
| `customer_id`     | 0.15   | Whether the sold-to / bill-to party is the same                 |
| `line_items`      | 0.20   | Similarity of SKUs, quantities, and line structure               |
| `amount`          | 0.10   | How closely the order total matches the original                 |
| `timestamp`       | 0.10   | Recency of the original PO (closer in time → higher score)      |
| `ship_to`         | 0.05   | Whether the ship-to address resolves to the same location        |
| `channel`         | 0.05   | Whether the order arrived via the same channel as the original   |
| `delivery_date`   | 0.05   | Whether the requested delivery dates align                       |

Weights sum to **1.0**.

### 3.1 Channel-Specific Signal Expectations

The recipe scores identically regardless of channel. However, the signal *profiles* tend to differ, which is useful context for human reviewers:

- **EDI orders** typically show high `po_number` match (exact retransmission of the same PO number) with consistent `ship_to` and `line_items`. The `channel` signal will be high (both EDI).
- **Manual entries** are more likely to have fuzzy `po_number` scores (typos, reformatted numbers) but high `customer_id` and `line_items` match. The `channel` signal may be low if the original was EDI but the duplicate was keyed manually, or vice versa.

These patterns are informational — the recipe does not branch on them.

### 3.2 Validation Rules

| Condition                                 | Action                                                                 |
|-------------------------------------------|------------------------------------------------------------------------|
| Any signal value outside [0.0, 1.0]       | Reject payload. Emit `SIGNAL_RANGE_INVALID`. Route to `FAIL_TO_HUMAN`. |
| Any signal value non-numeric              | Reject payload. Emit `SIGNAL_TYPE_INVALID`. Route to `FAIL_TO_HUMAN`.  |
| Fewer than 4 of 8 signals present         | Route to `FAIL_TO_HUMAN` with reason `INSUFFICIENT_SIGNALS`.           |
| 4–7 of 8 signals present                  | Proceed but flag `PARTIAL_SIGNAL_COVERAGE` in the execution log.       |
| All 8 signals present                     | Proceed normally.                                                      |

**Important:** The recipe defaults missing signals to 0.0 (conservative worst-case). A missing signal is treated as a non-match, not ignored. The `MISSING_SIGNALS` flag in the recipe output lists which signals were absent.

---

## 4. Recipe Interface Contract

The recipe is called as:

```python
from DuplicatePORecipe import detect_duplicate_po

result = detect_duplicate_po(
    incoming_po_number  = event.metadata.incoming_po_number,          # str, required
    customer_id         = event.metadata.customer_id,                 # str, required
    signal_scores       = event.metadata.signal_scores,               # Dict[str, float], required
    source_channel      = event.metadata.source_channel,              # "EDI" | "MANUAL", required
    prior_rejection_ref = event.metadata.prior_rejection_ref or None, # str | None, optional
    erp_warning_issued  = event.metadata.erp_warning_issued or False, # bool, optional
)
```

### 4.1 Recipe Output Schema

| Field                  | Type         | Always Present | Description                                              |
|------------------------|--------------|----------------|----------------------------------------------------------|
| `status`               | str          | Yes            | Operational state of the order (see Section 5)           |
| `composite_score`      | float        | Yes            | Weighted aggregate score, range [0.0, 1.0]               |
| `classification`       | str          | Yes            | Scoring tier label (see Section 5)                       |
| `recommended_action`   | str          | Yes            | Channel-agnostic action for this tier (see Section 5)    |
| `signal_breakdown`     | Dict[str, float] | Yes        | Weighted contribution per signal                         |
| `incoming_po_number`   | str          | Yes            | Echo of the input PO number                              |
| `customer_id`          | str          | Yes            | Echo of the input customer ID                            |
| `source_channel`       | str          | Yes            | Echo of the source channel (`EDI` or `MANUAL`)           |
| `erp_warning_issued`   | bool         | Yes            | Echo of the ERP warning flag                             |
| `prior_rejection_ref`  | str \| None  | Yes            | Echo of the prior rejection reference                    |
| `flags`                | List[str]    | Yes            | Contextual flags from validation and classification      |

### 4.2 What the Recipe Does NOT Do

The recipe is a pure scoring function. It does not:

- Place holds on orders in the ERP
- Send notifications to agents or queues
- Write annotations to order records
- Call external services

All of those are the orchestrator's responsibility, guided by the routing tables in Sections 8 and 9.

---

## 5. Classification Tiers and Constrained Values

These are the **only** valid values for machine-consumed output fields. Defined in `DuplicatePORecipe._TIERS` and mirrored here. Any value not in these sets is an error.

| Composite Score Range | Classification         | Status             | Recommended Action     |
|-----------------------|------------------------|--------------------|------------------------|
| 0.90 – 1.00          | `CONFIRMED_DUPLICATE`  | `HELD`             | `HOLD_AND_NOTIFY`      |
| 0.70 – 0.89          | `PROBABLE_DUPLICATE`   | `REVIEW_REQUIRED`  | `ESCALATE_TO_REVIEW`   |
| 0.50 – 0.69          | `SOFT_FLAG`            | `FLAGGED`          | `ANNOTATE_AND_RELEASE` |
| 0.00 – 0.49          | `NO_MATCH`             | `CLEARED`          | `RELEASE`              |

### 5.1 How `status` and `classification` Differ

- **`classification`** describes the *assessment* — how duplicate-like the order is.
- **`status`** describes the *operational state* — what has been done to the order.

They always pair from the same row. They never mix across rows.

### 5.2 Valid Flags

Flags are non-exclusive and accumulate.

| Flag                           | Meaning                                                         |
|--------------------------------|-----------------------------------------------------------------|
| `INSUFFICIENT_SIGNALS`         | Fewer than 4 of 8 signals were present.                         |
| `PARTIAL_SIGNAL_COVERAGE`      | 4–7 signals present; scoring proceeded with 0.0 defaults.      |
| `MISSING_SIGNALS:<csv>`        | Comma-separated list of absent signal keys.                     |
| `RESUBMISSION_CONTEXT`         | A `prior_rejection_ref` was provided.                           |
| `ERP_WARNING_OVERRIDDEN`       | The ERP issued a duplicate warning that the agent overrode.     |

---

## 6. Reasoning Loop

1. **Validate payload** against trigger conditions (Section 2) and signal requirements (Section 3).
2. **Classify intent** as `DUPLICATE_PO`. This is the only valid intent for this event type.
3. **Call Compliance Shadow** (Section 7) — must clear before recipe execution.
4. **If GREEN:** Call `detect_duplicate_po()` with the parameters defined in Section 4.
5. **Merge** compliance verdict + recipe result + source channel into a routing decision (Sections 8 and 9).
6. **Emit** the structured execution log (Section 12).

Do not reorder these steps. Do not call the recipe before the compliance gate clears.

---

## 7. Compliance Shadow Gate

The Compliance Shadow is a synchronous sidecar check called **before** recipe execution.

### 7.1 What It Checks

- **Sanctions screening:** Trading partner is not on a denied-party or restricted-entity list.
- **Credit hold:** The customer account is not in a credit-hold or collections state.
- **Contract compliance:** An active trading partner agreement covers the submitted item categories.
- **Duplicate suppression window:** The same PO number from the same partner has not already been accepted within the suppression window (default: 48 hours). This is the primary defense against EDI burst retransmissions.

### 7.2 How to Call It

```
POST /api/compliance-shadow/check
Body: {
  "trading_partner_id": event.metadata.trading_partner_id,
  "po_number":          event.metadata.incoming_po_number,
  "customer_id":        event.metadata.customer_id,
  "order_total":        event.metadata.order_total,
  "line_items":         event.metadata.line_items
}
Timeout: 5 seconds
```

### 7.3 Verdict Routing

| Verdict    | Action                                                                  |
|------------|-------------------------------------------------------------------------|
| `GREEN`    | Proceed to recipe execution (Step 4 of reasoning loop).                 |
| `YELLOW`   | Route to `MANUAL_REVIEW_REQUIRED`. Assemble the review bundle (7.4).    |
| `RED`      | Halt immediately. Log `shadow.breach_code` and `shadow.breach_description`. Emit `COMPLIANCE_HOLD`. Do not call the recipe. |

### 7.4 Manual Review Bundle (YELLOW Path)

When routing to manual review on a YELLOW verdict, assemble:

- **Summary:** Plain-language description (e.g., "Saved order PO 4501234 from Acme Corp flagged as potential duplicate of PO 4501200. Compliance raised a credit hold warning. Order entered via MANUAL channel; SAP duplicate warning was overridden during entry.").
- **Signal scores:** The full `event.metadata.signal_scores` object.
- **Compliance warnings:** The `shadow.warnings[]` array.
- **Original PO reference:** `event.metadata.original_po_ref`.
- **Source context:** `source_channel`, `erp_warning_issued`, `entering_agent_id` (if MANUAL).
- **Review queue:** `DUP_PO_REVIEW`. If the warning involves sanctions, route to `COMPLIANCE_ESCALATION` instead.

---

## 8. Merged Routing — Compliance × Recipe (Channel-Agnostic)

The compliance verdict and the recipe classification are two independent axes. This table defines the base routing decision *before* channel-specific adjustments (Section 9).

| Compliance | Recipe Classification    | Merged Routing                                                          |
|------------|--------------------------|-------------------------------------------------------------------------|
| `GREEN`    | `CONFIRMED_DUPLICATE`    | Place a hold on the saved order. Notify via channel-specific path (Section 9). |
| `GREEN`    | `PROBABLE_DUPLICATE`     | Place a hold. Route to review queue with recipe output + review bundle.  |
| `GREEN`    | `SOFT_FLAG`              | Allow fulfillment. Write a duplicate-risk annotation to the order record in the OMS/ERP. Annotation must include `composite_score`, `classification`, and `signal_breakdown`. |
| `GREEN`    | `NO_MATCH`               | Release. No annotation needed.                                          |
| `YELLOW`   | *(any)*                  | Route to `MANUAL_REVIEW_REQUIRED`. Recipe is **not called**.             |
| `RED`      | *(any)*                  | `COMPLIANCE_HOLD`. Recipe is **not called**.                             |

---

## 9. Channel-Specific Routing

The recipe produces the same score and classification regardless of channel. The orchestrator uses `source_channel` and the optional metadata fields to determine **who is notified, which queue is used, and what context is attached**.

### 9.1 EDI Channel

| Recipe Classification    | Queue                    | Notification Target           | Bundle Includes                                              |
|--------------------------|--------------------------|-------------------------------|--------------------------------------------------------------|
| `CONFIRMED_DUPLICATE`    | `DUP_PO_EXCEPTION`       | Exception management team     | Recipe output, original PO ref, signal breakdown, EDI transaction ID |
| `PROBABLE_DUPLICATE`     | `DUP_PO_REVIEW`          | Exception management team     | Recipe output, original PO ref, signal breakdown              |
| `SOFT_FLAG`              | *(no queue — annotate)*  | *(no notification)*           | Annotation written to order record                            |
| `NO_MATCH`               | *(none)*                 | *(none)*                      | *(none)*                                                      |

### 9.2 MANUAL Channel

| Recipe Classification    | Queue                    | Notification Target           | Bundle Includes                                              |
|--------------------------|--------------------------|-------------------------------|--------------------------------------------------------------|
| `CONFIRMED_DUPLICATE`    | `DUP_PO_EXCEPTION`       | Entering agent + team lead    | Recipe output, original PO ref, signal breakdown, `entering_agent_id`, `erp_warning_issued` |
| `PROBABLE_DUPLICATE`     | `DUP_PO_REVIEW`          | Entering agent + team lead    | Recipe output, original PO ref, signal breakdown, `entering_agent_id`, `erp_warning_issued` |
| `SOFT_FLAG`              | *(no queue — annotate)*  | Entering agent (info only)    | Annotation written to order record; lightweight notification to the agent who entered it |
| `NO_MATCH`               | *(none)*                 | *(none)*                      | *(none)*                                                      |

### 9.3 ERP Warning Escalation

When `erp_warning_issued` is `true` **and** the recipe classifies as `CONFIRMED_DUPLICATE` or `PROBABLE_DUPLICATE`, this means two independent systems — the ERP's built-in check and this skill's multi-signal scoring — both flagged the same order. This is a high-confidence signal.

In this case:

- Add the `ERP_WARNING_OVERRIDDEN` flag to the review bundle (the recipe already includes it in `flags`).
- Prepend the review summary with: "NOTE: SAP issued a duplicate PO warning during entry which was overridden by the entering agent."
- If local policy requires it, CC the entering agent's manager on the notification. (This is configurable per deployment — the skill does not enforce it.)

---

## 10. Edge Cases

### 10.1 Same PO Number, Different Ship-To

High `po_number` score but low `ship_to` score. Common in multi-location retailers issuing POs from a central procurement system. The recipe may classify as `NO_MATCH` or `SOFT_FLAG`. The agent should not override but should ensure relevant flags are logged.

### 10.2 Revised Quantities on Resubmission

High `po_number` and `customer_id` scores, moderate `line_items` and `amount`. Often a PO revision rather than a true duplicate. If classified as `PROBABLE_DUPLICATE`, the review bundle should highlight quantity and amount deltas so the reviewer can determine if this is an intentional revision.

### 10.3 PO Resubmission After Prior Hold/Rejection

If `event.metadata.prior_rejection_ref` is present, pass it to the recipe's `prior_rejection_ref` parameter. The recipe will include a `RESUBMISSION_CONTEXT` flag. The reviewer needs this context to distinguish a legitimate retry from a true duplicate.

### 10.4 Burst Submissions (EDI Retransmit)

Multiple copies of the same PO arriving within a short window (< 60 seconds) due to EDI retry logic. The Compliance Shadow's duplicate suppression window (Section 7.1) is the primary defense. Only the first proceeds; subsequent copies are tagged `EDI_RETRANSMIT_SUPPRESSED` and auto-held.

### 10.5 Cross-Channel Duplicate

The original order entered via EDI and the duplicate was keyed manually (or vice versa). The `channel` signal score will be low, which reduces the composite score. This is correct behavior — cross-channel submission patterns are less likely to be accidental duplicates. However, if the other signals are strong (high `po_number`, `customer_id`, `line_items`), the composite may still reach `PROBABLE_DUPLICATE`. The review bundle should note the channel mismatch for the reviewer.

### 10.6 Agent Re-Entry After EDI Failure

A customer's EDI submission fails, and the customer calls in to place the order manually. This looks like a cross-channel duplicate but is actually a legitimate order. The `channel` signal will be low (EDI vs. MANUAL), which helpfully reduces the composite. If the EDI order was never successfully saved (i.e., no `original_po_ref` exists for the EDI attempt), this skill should not fire at all — there's nothing to match against. If the EDI order *was* partially saved before failing, the reviewer needs the failure context to make the right call.

---

## 11. Error and Timeout Handling

| Failure Scenario                              | Status Code                    | Routing                       |
|-----------------------------------------------|--------------------------------|-------------------------------|
| Compliance Shadow times out (> 5s)            | `SHADOW_UNAVAILABLE`           | `MANUAL_REVIEW_REQUIRED`      |
| Compliance Shadow returns non-200             | `SHADOW_ERROR`                 | `MANUAL_REVIEW_REQUIRED`      |
| Compliance Shadow returns unexpected verdict  | `SHADOW_VERDICT_INVALID`       | `MANUAL_REVIEW_REQUIRED`      |
| Recipe raises `DuplicatePOValidationError`    | `RECIPE_VALIDATION_FAILURE`    | `FAIL_TO_HUMAN`               |
| Recipe raises any other exception             | `RECIPE_EXECUTION_FAILURE`     | `FAIL_TO_HUMAN`               |
| Recipe returns unexpected classification      | `RECIPE_OUTPUT_INVALID`        | `FAIL_TO_HUMAN`               |
| Recipe timeout (> 10s)                        | `RECIPE_TIMEOUT`               | `FAIL_TO_HUMAN`               |
| Payload missing required fields               | `PAYLOAD_VALIDATION_FAILURE`   | `FAIL_TO_HUMAN`               |
| Signal score out of [0.0, 1.0]                | `SIGNAL_RANGE_INVALID`         | `FAIL_TO_HUMAN`               |
| Invalid `source_channel` value                | `CHANNEL_INVALID`              | `FAIL_TO_HUMAN`               |

**Principle:** No failure may result in a silently stalled order. The order is already in the system — a stalled exception process means an order sits in limbo without anyone knowing. Every path produces a logged status, a human-readable reason, and a routing decision.

### 11.1 Anomaly Detection

If the recipe returns a classification grossly inconsistent with the composite score (e.g., `CONFIRMED_DUPLICATE` at 0.15, or `NO_MATCH` at 0.95), emit `RECIPE_OUTPUT_ANOMALY` and route to `MANUAL_REVIEW_REQUIRED`. Reference the score ranges in Section 5 for comparison.

---

## 12. Execution Log (Output Requirements)

Every execution — success, failure, or escalation — must emit a log entry with the following fields:

| Field                    | Type         | Required | Source                                                       |
|--------------------------|--------------|----------|--------------------------------------------------------------|
| `timestamp`              | ISO 8601     | Yes      | Generated at execution time                                  |
| `event_id`               | str          | Yes      | `event.event_id`                                             |
| `event_type`             | str          | Yes      | `EDI_850_DUPLICATE_PO` or `MANUAL_ENTRY_DUPLICATE_PO`        |
| `source_channel`         | str          | Yes      | `EDI` or `MANUAL`                                            |
| `trading_partner_id`     | str          | Yes      | `event.metadata.trading_partner_id`                          |
| `incoming_po_number`     | str          | Yes      | `event.metadata.incoming_po_number`                          |
| `customer_id`            | str          | Yes      | `event.metadata.customer_id`                                 |
| `original_po_ref`        | str          | Yes      | `event.metadata.original_po_ref`                             |
| `erp_order_number`       | str          | No       | `event.metadata.erp_order_number` (if available)             |
| `composite_score`        | float        | Yes*     | `result.composite_score`                                     |
| `classification`         | str          | Yes*     | `result.classification` — must be one of Section 5 values    |
| `status`                 | str          | Yes*     | `result.status` — must be one of Section 5 values            |
| `recommended_action`     | str          | Yes*     | `result.recommended_action` — must be one of Section 5 values|
| `signal_breakdown`       | Dict         | Yes*     | `result.signal_breakdown`                                    |
| `shadow_verdict`         | str          | Yes      | `GREEN`, `YELLOW`, `RED`, or a `SHADOW_*` error code         |
| `merged_routing`         | str          | Yes      | The final routing decision from Section 8                    |
| `notification_target`    | str          | Yes      | Who was notified, from Section 9                             |
| `queue`                  | str          | No       | Which queue the exception was routed to (if applicable)      |
| `erp_warning_issued`     | bool         | No       | `true` if SAP/ERP warning was overridden (MANUAL channel)    |
| `entering_agent_id`      | str          | No       | Agent who entered the order (MANUAL channel)                 |
| `flags`                  | List[str]    | No       | Combined flags from recipe + orchestrator                    |
| `prior_rejection_ref`    | str          | No       | `result.prior_rejection_ref` if present                      |
| `error`                  | Dict         | No       | Present only on failure: `{error_code, error_message}`       |

*Fields marked with `*` are required when the recipe was executed. They are absent (null) when the recipe was not called (YELLOW/RED compliance, or pre-recipe failure).

---

## 13. Recipe-to-Intent Mapping

| Intent            | Recipe                    |
|-------------------|---------------------------|
| `DUPLICATE_PO`    | `DuplicatePORecipe.py`    |
| Any other intent  | `FAIL_TO_HUMAN`           |

No other recipe is permitted for this event type.

---

## 14. Constrained Generation Policy

Use Guidance / Outlines for all machine-consumed outputs.

| Field                  | Allowed Values                                                                  |
|------------------------|---------------------------------------------------------------------------------|
| `intent`               | `DUPLICATE_PO`                                                                  |
| `recipe`               | `DuplicatePORecipe.py`                                                          |
| `shadow_verdict`       | `GREEN`, `YELLOW`, `RED`                                                        |
| `classification`       | `CONFIRMED_DUPLICATE`, `PROBABLE_DUPLICATE`, `SOFT_FLAG`, `NO_MATCH`            |
| `status`               | `HELD`, `REVIEW_REQUIRED`, `FLAGGED`, `CLEARED`                                |
| `recommended_action`   | `HOLD_AND_NOTIFY`, `ESCALATE_TO_REVIEW`, `ANNOTATE_AND_RELEASE`, `RELEASE`      |
| `source_channel`       | `EDI`, `MANUAL`                                                                 |

Any output value not in these sets must be treated as an error.

---

## 15. Guardrails Summary

1. **Post-commit only.** This skill never prevents order entry. It holds, annotates, or clears orders that already exist.
2. **One intent, one recipe.** No branching, no recipe selection logic to hallucinate.
3. **Gate before execute.** The Compliance Shadow must clear GREEN before the recipe runs.
4. **Shared contract.** Signal keys, classification labels, status values, and action values are defined identically in this doc and in `DuplicatePORecipe.py`. If either changes, the other must be updated.
5. **Channel-agnostic scoring, channel-specific routing.** The recipe scores the same way for EDI and MANUAL. The orchestrator routes differently based on channel (Section 9).
6. **ERP warning is a corroborating signal, not a scoring input.** If SAP warned and the agent overrode, that fact is captured and surfaced to reviewers — but it does not inflate the composite score. Two independent systems flagging the same order is noted, not double-counted.
7. **Fail loud, never silent.** Every path produces a logged, routed outcome. A stalled exception on a saved order is worse than a false positive.
8. **Agent does not score.** The recipe scores. The agent orchestrates, validates, and audits.
9. **Constrained outputs only.** All machine-consumed fields are drawn from fixed enumerations.
10. **Echo inputs for auditability.** The recipe echoes `incoming_po_number`, `customer_id`, `source_channel`, `erp_warning_issued`, and `prior_rejection_ref` so the execution log is self-contained.
