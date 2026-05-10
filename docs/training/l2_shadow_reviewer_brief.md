# Reviewer Training Brief — L2 LLM Shadow Verdicts

**Audience:** CSR managers + analysts who handle MANUAL_REVIEW_REQUIRED queue.
**Owner:** Domain SME.
**Trigger:** Required reading **before** the ADR-039 X.2 ratification flip lands.
**Time budget:** 20 minutes for the body; 30 minutes for the worked examples.

---

## 1. What changes for you

### 1.1 Today (X.1, observe-only)

When you open a record in the MANUAL_REVIEW queue, the reasons block under "Compliance evaluation" comes from the **rule-based** Compliance Shadow alone. Every reason you see is traceable to a deterministic rule in `compliance/shadow.py`.

### 1.2 After X.2 ratification

The Compliance evaluation block will sometimes carry an additional reason prefixed with `LLM_SHADOW:`. Example:

```
Compliance evaluation
─────────────────────
• Rule MK-2026-12: order amount $12,400 above auto-approve threshold.
• LLM_SHADOW: customer's blanket-PO release schedule was missed by
  the duplicate-PO rule.
  Concerns: BLANKET_PO_RELEASE_MISIDENTIFIED
```

The `LLM_SHADOW:` prefix means a second LLM-based gate looked at the same case and surfaced an additional concern the rules missed. Concerns come from a **closed vocabulary** — they are not free-form.

### 1.3 What hasn't changed

* RED records still BLOCK the same way. The LLM never escalates GREEN to RED — only to YELLOW.
* The deterministic verdict still drives the lifecycle. The LLM can move GREEN → YELLOW; it cannot move YELLOW → GREEN. Asymmetric authority is structural.
* Your decision authority is unchanged. Approve / reject / escalate work the same.

---

## 2. How to read an LLM_SHADOW reason

Every LLM-driven reason has three readable parts:

| Part | Where it comes from | Why you care |
|---|---|---|
| `reason` | One-sentence rationale, ≤200 chars, surfaced verbatim. | Plain-English explanation; tells you what the LLM noticed. |
| `policy_concerns` | Closed vocabulary; comma-separated list. | Each entry is auditable — query "show me every case with HIGH_VALUE_THRESHOLD_PROXIMITY" works. |
| Confidence | Internal; not surfaced on the queue but visible in the audit-trace. | Higher confidence ≠ "approve it"; just means the LLM was sure of its observation, not that the observation is correct. |

The closed `policy_concerns` vocabulary lives in `knowledge/shadow_llm/concerns_vocabulary.yaml`. The 12 X.1 seed concerns:

| Concern | What it means |
|---|---|
| `CUSTOMER_OPT_OUT_VIOLATION` | Customer has an account-level opt-out the rules didn't honour. |
| `BLANKET_PO_RELEASE_MISIDENTIFIED` | A blanket-PO release was treated as a duplicate (or vice versa). |
| `CONTRACT_BAND_VIOLATION` | Pricing decision contradicts a customer-specific commercial agreement. |
| `SKU_FAMILY_SUBSTITUTION_RESTRICTED` | Substitution for a SKU the customer disallowed. |
| `HIGH_VALUE_THRESHOLD_PROXIMITY` | Amount suspiciously close to (just under) a cosign threshold. |
| `REPEATED_NEAR_THRESHOLD_PATTERN` | Several near-threshold cases on same customer in short window. |
| `PRIOR_CASE_DECISION_CONTRADICTION` | Action contradicts something already decided on this case. |
| `AUDIT_EVIDENCE_INSUFFICIENT` | Recipe is missing an audit-bearing field that should be present for this action. |
| `NOVEL_CUSTOMER_BEHAVIOUR` | Customer's pattern is out-of-distribution vs the case history. |
| `AUTOMATION_BOUNDARY_CONCERN` | Action would cross an autonomy-level boundary the customer profile doesn't permit. |
| `REGULATORY_HOLD_INDICATION` | Recipe params or context reference a regulatory term the deterministic rules don't model. |
| `CONFIDENCE_INSUFFICIENT` | Used with `ABSTAIN` action — LLM declined to take a position. |

---

## 3. How to disposition an LLM_SHADOW-downgraded case

The LLM is **conservative by design**. The system prompt explicitly biases ABSTAIN over AGREE, and DISAGREE_DOWNGRADE only when a named concern fits. Your job is to:

1. **Read the rule reason first.** The deterministic rule is the floor; if the rule fires, your disposition follows the rule.
2. **Read the LLM concern second.** Ask: "Does the named concern match what I'd flag as a real issue?"
3. **Look at the named vocabulary entry, not the prose.** The vocabulary is the auditable signal; the prose is colour.
4. **Decide:**
   - If the LLM concern is a **real catch** the rules missed → sustain the YELLOW verdict; route as you would for any rule-driven YELLOW.
   - If the LLM concern is **not applicable** (you can articulate why) → override to APPROVED. The override is recorded; a high false-downgrade rate triggers the runbook (`docs/runbooks/shadow_llm_x2_rollback.md`).

### Anti-patterns

* **Don't auto-approve every LLM_SHADOW** because "the rules already said GREEN". The whole point of the L2 gate is to catch what the rules missed.
* **Don't auto-sustain every LLM_SHADOW** because "the LLM said so". The LLM is sometimes wrong; your override authority is the safety net.
* **Don't argue with the named concern in the prose.** If the prose feels off but the named concern fits, the named concern is what the audit log captures. Talk to your manager about whether the vocabulary needs an update; don't override against the audit trail.

---

## 4. Worked examples

### 4.1 Reverse-disagreement (LLM cried wolf)

```
Order:           PO-2026-08882, $11,400, Acme Distributors.
Rule verdict:    GREEN (price within tolerance band).
LLM verdict:     DISAGREE_DOWNGRADE
LLM reason:      "Order $11,400 just under $15k cosign threshold;
                  Acme has 3 similar orders in last 7 days."
Concerns:        REPEATED_NEAR_THRESHOLD_PATTERN
```

**Action:** Approve override. The 3-orders-in-7-days pattern is real but Acme has a documented bulk-purchasing arrangement (visible in their account profile). Document the override reason as `customer_contractual_pattern`. The audit log captures both the LLM concern and your override.

### 4.2 Sustained-disagreement (LLM caught a real one)

```
Order:           PO-2026-09001, $4,800, MegaMart.
Rule verdict:    GREEN (substitution within same SKU family).
LLM verdict:     DISAGREE_DOWNGRADE
LLM reason:      "Substitution within bottle-size family; MegaMart
                  account has restrict_substitution=true on SKU-fam-3."
Concerns:        SKU_FAMILY_SUBSTITUTION_RESTRICTED
```

**Action:** Sustain the YELLOW. Confirm the customer's restrict_substitution flag in their profile. Reach out to the buyer to confirm acceptable substitute. Document the disposition as `customer_opt_out_honoured`.

### 4.3 ABSTAIN (LLM declined to take a position)

```
Order:           PO-2026-09114, $750, BigBox Co.
Rule verdict:    GREEN.
LLM verdict:     ABSTAIN
LLM reason:      "Insufficient context to take a position."
Concerns:        CONFIDENCE_INSUFFICIENT
```

**Action:** No additional action. Lifecycle stays GREEN; the case auto-resolves per the rule verdict. The ABSTAIN is recorded in the audit log for telemetry but does not move the verdict.

---

## 5. What to escalate

* **Any case where you believe the LLM concern is wrong but the customer profile doesn't support an override.** This is exactly the data the X.2 → X.3 ratification gate looks at. Forward to your manager + Compliance contact.
* **Any case where the LLM concern is correct but not in the closed vocabulary.** This is the signal the vocabulary needs an update. Note the case_id + a short rationale in `#asoe-shadow-vocabulary` Slack channel.
* **A burst of similar LLM_SHADOW concerns (≥5 in 30 min on same intent).** May indicate a rule-drift the rules team should investigate. Page the on-call backend engineer.

---

## 6. Where the audit trail lives

Every case that L2 Shadow touched carries:

* The deterministic rule verdict.
* The LLM verdict (action / reason / confidence / policy_concerns).
* The combiner result (which one won; with the X.2 truth table this is deterministic given the inputs).
* The reviewer disposition + reason_code.

You can pull the full chain from the Audit tab on the case detail. Compliance auditors can query the same data via the audit-bearing registry.

---

## 7. Pre-X.2 quiz (informal; for your own learning)

Skim the worked examples above, then answer:

1. The LLM never moves a GREEN verdict to ___? (Answer: RED — asymmetric authority is structural; only DOWNGRADE to YELLOW.)
2. Which verdict means "the LLM declined to take a position"? (Answer: ABSTAIN.)
3. Which closed-vocabulary concern fits "the customer has an account-level opt-out the rules didn't honour"? (Answer: CUSTOMER_OPT_OUT_VIOLATION.)
4. If you override a LLM_SHADOW downgrade, the audit log records: (a) only your decision, (b) only the LLM concern, (c) both. (Answer: c.)
5. The runbook fires when the false-downgrade rate exceeds: (a) 10%, (b) 25%, (c) 40%. (Answer: c — sustained 30 min.)

---

*This brief is the X.2 ratification training material. Domain SME owns updates; revisit before each phase ratification (X.3 → X.4 will add the cross-check extension and need a §1 update).*
