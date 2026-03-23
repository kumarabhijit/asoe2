---
name: s-03-customer-communication-composer
description: >
  AI-powered customer communication generation for ASOE broken layer/pallet exceptions. Drafts customer-
  facing emails, internal CSR notes, and PO correction requests when pallet validation requires human
  action. Calibrates tone by customer tier and relationship context — Costco rejections are compliance-
  focused, Kroger surcharge notices are collaborative, Walmart round-up proposals are options-based.
  Triggers when R-03 returns PROPOSE_ALTERNATIVES, REJECT_LINE, or REJECT_OR_SURCHARGE. Also generates
  optimization notifications when S-02 recommends mixed-pallet fulfillment. Includes dollar impact,
  alternative quantities, and clear next steps in every message.
---

# S-03 — Customer Communication Composer

## 1. Problem domain

When the pallet validation workflow determines that a customer's order cannot be fulfilled as-submitted,
someone needs to communicate this to the customer. Today, CSRs manually draft these emails — often
inconsistently, missing key details (dollar impact, alternative quantities), or using inappropriate
tone for the customer relationship.

This skill generates ready-to-send communications that are factually precise (grounded in recipe
outputs), tonally appropriate (calibrated to customer tier), and actionable (clear next steps with
specific quantities and costs).

### Why this is a skill, not a recipe

- Tone calibration requires understanding customer relationship context — a strategic account gets
  a different communication style than a transactional one.
- Dollar impact framing varies — some customers respond better to "your order increases by $44" while
  others prefer "only 3 additional cases needed to complete the pallet."
- The CSR internal notes require synthesis of the full validation chain (rule matched, why alternatives
  were proposed, what the customer's historical pattern looks like).
- Different action types (propose alternatives, reject, notify surcharge, recommend optimization)
  require fundamentally different message structures.

## 2. Communication types

### Type 1 — Propose alternatives (PROPOSE_ALTERNATIVES)
Customer has options: round up or round down. Email presents both with dollar impact and recommends
the option that best fits their typical ordering pattern.

### Type 2 — PO rejection / correction request (REJECT_LINE)
Customer's PO quantity cannot be fulfilled per their own compliance requirements (e.g., Costco full-
pallet policy). Email cites the policy, provides valid quantities, and requests a corrected PO.

### Type 3 — Surcharge notification (PASS_WITH_SURCHARGE)
Customer's order will be processed but a broken-layer surcharge applies per their trading agreement.
Email confirms the order, explains the surcharge calculation, and references the agreement.

### Type 4 — Mixed pallet optimization notification (OPTIMIZE_MIXED_PALLET)
ASOE has identified a more efficient fulfillment method. Email explains the consolidation, shows
savings, and confirms unless the customer objects.

## 3. Inputs

```json
{
  "communication_type": "PROPOSE_ALTERNATIVES",
  "order_context": {
    "vbeln": "0000088430",
    "bstnk": "WM-PO-2026-44825",
    "customer_name": "Walmart Inc.",
    "customer_tier": "A",
    "customer_contact": "procurement@walmart.com",
    "sales_rep": "Jane Davis",
    "audat": "2026-03-22",
    "vdatu": "2026-03-28"
  },
  "line_context": {
    "posnr": "000010",
    "matnr": "000000000000050042",
    "maktx": "12-pk Cola 355ml",
    "sku": "SKU-0042",
    "original_qty": 47,
    "uom": "CS"
  },
  "validation_result": {
    "action": "PROPOSE_ALTERNATIVES",
    "rule_matched": "BLP-005",
    "reason": "Broken layer — remainder 7 cases (70% of layer). Auto-round tolerance exceeded."
  },
  "rounding_options": {
    "round_up": { "qty": 50, "delta_qty": 3, "delta_pct": 6.38, "delta_dollars": 44.64 },
    "round_down": { "qty": 40, "delta_qty": -7, "delta_pct": -14.89, "delta_dollars": -104.16 }
  },
  "customer_policy": {
    "require_full_layer": true,
    "auto_round_up_tolerance_pct": 5,
    "notes": "Walmart requires full layer minimum."
  }
}
```

## 4. Output structure

```json
{
  "email": {
    "to": "procurement@walmart.com",
    "cc": "jane.davis@ourcompany.com",
    "subject": "Action needed: PO WM-PO-2026-44825 — quantity adjustment for SKU-0042",
    "body": "...",
    "priority": "normal",
    "requires_response": true,
    "response_deadline": "2026-03-25"
  },
  "csr_notes": {
    "summary": "Walmart PO-44825 line 10: 47 CS Cola does not align to full layer (Ti=10). Auto-round exceeded 5% tolerance (would need 6.38%). Two options presented — round up to 50 (+$44.64) or round down to 40 (-$104.16). Walmart typically rounds up on Cola orders based on last 6 months of history.",
    "recommended_option": "Round up to 50 CS",
    "recommendation_basis": "Historical pattern: 4 of last 5 similar adjustments on this account were round-ups. Dollar impact is minimal ($44.64 on a $700+ line).",
    "escalation_needed": false
  },
  "audit_reference": {
    "communication_id": "COMM-88430-010",
    "generated_at": "2026-03-22T14:32:10Z",
    "agent": "ASOE-S03-v1.0",
    "validation_rule": "BLP-005"
  }
}
```

## 5. Tone calibration rules

### Tier A — Strategic accounts (Walmart, Target, Costco)
- Formal but direct
- Reference specific policy or agreement terms
- Present options with clear business rationale
- Never apologize for enforcing the customer's own compliance rules
- Include specific dollar amounts and quantities — these buyers operate on precision

### Tier B — Key accounts (Kroger, Albertsons)
- Professional and collaborative
- Frame surcharges as standard terms, not penalties
- Offer context ("as outlined in our distribution agreement dated...")
- Suggest process improvements to avoid future exceptions

### Tier C — Standard accounts
- Friendly and helpful
- Explain the layer/pallet concept briefly (they may not know the terminology)
- Recommend the simplest resolution path
- Offer to discuss by phone if the options are confusing

## 6. Email templates by type

### PROPOSE_ALTERNATIVES template structure
```
Subject: Action needed: PO {bstnk} — quantity adjustment for {sku}

Hi {customer_contact_name},

We're processing your PO {bstnk} and noticed that line {posnr} ({maktx}, {original_qty} {uom})
doesn't align to a full layer quantity.

We have two options to resolve this efficiently:

Option A — Round up to {round_up_qty} {uom}
  Additional: {delta_qty_up} cases (+${delta_dollars_up})

Option B — Round down to {round_down_qty} {uom}
  Reduction: {delta_qty_down} cases (-${delta_dollars_down})

[If applicable: "Based on your recent ordering pattern, Option A aligns with your typical volumes."]

Could you confirm your preference by {response_deadline}? We'll hold this line until we hear from you
— all other lines on this PO are processing normally.

{sales_rep_name}
{company_name}
```

### REJECT_LINE template structure
```
Subject: PO correction needed: {bstnk} — {sku} quantity does not meet pallet requirements

Hi {customer_contact_name},

Per your distribution compliance requirements, PO {bstnk} line {posnr} ({maktx}, {original_qty} {uom})
cannot be fulfilled — quantities must align to full pallet multiples of {full_pallet_qty} cases.

The nearest valid quantities are:
  • {nearest_down} {uom} (${nearest_down_value})
  • {nearest_up} {uom} (${nearest_up_value})

Please submit a corrected PO with an adjusted quantity. All other lines on this order are
{processing_normally_or_also_affected}.

{sales_rep_name}
```

### PASS_WITH_SURCHARGE template structure
```
Subject: Order confirmation: {bstnk} — broken layer handling applied

Hi {customer_contact_name},

Your PO {bstnk} has been processed. Line {posnr} ({maktx}, {original_qty} {uom}) includes
{broken_cases} cases in a partial layer. Per our distribution agreement (effective {agreement_date}),
a {surcharge_pct}% broken layer handling fee of ${surcharge_amount} has been applied to those cases.

Order total: ${order_total} (including ${surcharge_amount} handling).

No action needed on your end — this order is confirmed for delivery on {vdatu}.

{sales_rep_name}
```

## 7. CSR internal notes

Every communication includes internal notes that the CSR can review before sending. Notes include:
- One-sentence summary of the exception
- Why this specific action was chosen (rule reference)
- Recommended option with reasoning (if alternatives exist)
- Customer historical pattern for this type of exception
- Whether escalation is needed (e.g., dollar impact > $5000, strategic account dispute)
- Relevant customer contact preferences (email vs. phone vs. EDI acknowledgment)

## 8. Edge cases

| Case | Behavior |
|---|---|
| Customer has no email on file | Generate communication but flag for CSR to find contact. Set delivery = "MANUAL". |
| Multiple lines need communication | Bundle into single email with per-line sections. Don't send 4 separate emails. |
| Dollar impact < $10 | Use simplified language. Don't over-dramatize a $3 adjustment. |
| Dollar impact > $5000 | Flag for supervisor review before sending. Add escalation note. |
| Customer language preference ≠ English | Note in CSR instructions. Future: generate in customer's preferred language. |
| Order is urgent (delivery within 48h) | Increase email priority. Shorten response deadline. Add CSR note about urgency. |
| Communication for mixed-pallet optimization | Positive framing — "we found a way to improve your shipment efficiency." |

## 9. Dependencies

- Upstream: R-03 (action type), R-04 (rounding options and dollar amounts), R-02 (customer policy for tone), S-02 (optimization results if applicable).
- Called by: S-01 Orchestrator (per line or per order depending on bundling).
- Downstream: Email draft is placed in ASOE outbox for CSR review and send.

## 10. Metrics

| Metric | Description |
|---|---|
| comms_generated | Total communications drafted |
| comms_by_type | Breakdown by PROPOSE/REJECT/SURCHARGE/OPTIMIZE |
| csr_edit_rate | % of drafts modified by CSR before sending (measures draft quality) |
| response_time_hours | Average customer response time after email sent |
| option_a_vs_b_rate | Which alternative customers choose more often (feeds R-03 tuning) |
| escalation_rate | % of communications flagged for supervisor review |
