> **Status: FUTURE — not implemented in V1.**
>
> Per **ADR-032 (Calibration Deferral and Future-State Contract)**, calibration is deferred. This document describes the eventual calibration program; customer-supplied calibrated values enter the system today via the **ADR-030** 5-level config override hierarchy.
>
> **Do not implement any portion of this document in V1.** It is preserved verbatim as a forward-looking reference to be re-scoped when ADR-032's re-opening conditions fire.

---

# Weight Calibration Methodology — Duplicate PO Detection

Read this file when deploying the duplicate PO check for a new customer.
The default weights in Section 2.1 of `docs/specs/duplicate-po-product-spec.md` are starting-point heuristics.
Every production deployment must go through the three calibration phases below.

---

## Why Default Weights Are Not Production Weights

The 8-signal weight distribution (PO Number 0.30, Line Items 0.20, etc.) is designed
to be a safe starting position that will catch obvious duplicates without excessive
false positives across a wide range of B2B order profiles. However, duplicate patterns
vary dramatically by:

- **Customer size** — A Walmart with 50,000 POs/month has different patterns than a regional chain doing 200/month
- **PO discipline** — Some buyers assign globally unique PO numbers; others reuse numbers quarterly
- **Channel mix** — Customers using only EDI have different duplicate profiles than those mixing EDI + email + portal
- **Product mix** — Customers ordering the same 50 SKUs repeatedly look different from those with highly variable baskets
- **Industry vertical** — Grocery/CPG vs. industrial distribution vs. pharma each have distinct ordering patterns

---

## Phase 1 — Baseline Calibration (Pre-Launch, 2–4 Weeks)

### Goal
Fit the scoring weights to the customer's actual duplicate patterns using historical data.

### Prerequisites
- 3–6 months of historical order data from the customer's OMS/ERP
- Known duplicate records: credit memos, return authorizations, exception logs, or ops team manual tagging
- If no labeled duplicate data exists, have the customer's ops team tag 200–500 historical orders (duplicate vs. legitimate)

### Process

```
1. EXTRACT historical orders
   │  Pull PO header + line items for the lookback period
   │  Source: SAP VBAK/VBAP, Oracle PO_HEADERS_ALL, or OMS API
   │
   ▼
2. LABEL ground truth
   │  Tag each order pair as:
   │    TRUE_DUPLICATE   — confirmed duplicate (credit memo, return, ops tag)
   │    LEGITIMATE       — confirmed distinct orders
   │    AMBIGUOUS        — unclear (exclude from training)
   │
   ▼
3. GENERATE candidate pairs
   │  For each order, find all other orders within the lookback window
   │  that share customer_id OR po_number_norm
   │  Compute raw signal scores for each pair (before weighting)
   │
   ▼
4. FIT weights via logistic regression
   │  X = 8 raw signal scores per pair
   │  Y = 1 (TRUE_DUPLICATE) or 0 (LEGITIMATE)
   │  Output: optimized coefficients = new signal weights
   │  Normalize coefficients to sum to 1.00
   │
   ▼
5. OPTIMIZE thresholds
   │  Using the fitted model, compute composite scores for all pairs
   │  Generate precision/recall curve
   │  Set thresholds:
   │    AUTO_BLOCK   = score where precision ≥ 0.98 (almost no false positives)
   │    REVIEW       = score where recall ≥ 0.95 (catch almost all duplicates)
   │    SOFT_FLAG    = score where recall ≥ 0.99 (safety net)
   │
   ▼
6. VALIDATE on held-out set
   │  70/30 train/test split
   │  Report: detection rate, false positive rate, precision, recall, F1
   │  Target: detection ≥ 95%, false positive ≤ 5%
   │
   ▼
7. SAVE as customer-specific configuration
   Store in config-defaults.json under customer_specific_overrides
```

### Example: Calibrated Weights for Different Customer Profiles

| Signal              | Default | Walmart (high-vol EDI) | Regional Grocer (mixed channel) | SMB (PO reuse) |
|--------------------:|--------:|-----------------------:|--------------------------------:|---------------:|
| PO Number           |    0.30 |                   0.40 |                            0.25 |           0.10 |
| Customer Account    |    0.15 |                   0.10 |                            0.15 |           0.20 |
| Line Item SKUs      |    0.20 |                   0.15 |                            0.20 |           0.35 |
| Order Total Amount  |    0.10 |                   0.10 |                            0.10 |           0.10 |
| Submission Timestamp|    0.10 |                   0.12 |                            0.10 |           0.10 |
| Ship-To Address     |    0.05 |                   0.05 |                            0.05 |           0.05 |
| Submission Channel  |    0.05 |                   0.03 |                            0.10 |           0.05 |
| Delivery Date       |    0.05 |                   0.05 |                            0.05 |           0.05 |

**Rationale for Walmart profile:** PO numbers are highly reliable (globally unique, never reused), so PO Number weight increases. They rarely multi-channel submit, so Channel weight drops. Line Items drops because their standard reorders have identical SKU sets — making it a weaker differentiator.

**Rationale for SMB profile:** PO numbers are frequently reused (sometimes just sequential integers), so PO Number weight drops dramatically. Line Item SKUs become the primary differentiator — if two orders have the same PO number but completely different products, it's clearly not a duplicate.

---

## Phase 2 — Supervised Learning Period (Days 1–60 Live)

### Goal
Validate and refine the calibrated weights against live production data using human feedback.

### Autonomy Constraints
During this phase, the system MUST operate at restricted autonomy:
- All detections run at **L1 (Observe)** or **L2 (Recommend)** — no auto-blocking
- Every detection requires human resolution
- Override reason is mandatory (not optional)

### Override Tracking Schema

Every human override generates a calibration data point:

```json
{
  "check_result_id": "dup_abc123",
  "agent_recommendation": "BLOCK_AND_NOTIFY",
  "human_decision": "ALLOW_BOTH",
  "override_reason": "INTENTIONAL_REORDER",
  "override_category": "FALSE_POSITIVE",
  "composite_score": 0.87,
  "signal_breakdown": {
    "po_number": 1.0,
    "customer_id": 1.0,
    "line_items": 0.95,
    "amount": 0.98,
    "timestamp": 0.60,
    "ship_to": 1.0,
    "channel": 0.80,
    "delivery_date": 0.40
  },
  "analyst_id": "user_jdoe",
  "customer_id": "cust_walmart",
  "timestamp": "2026-04-15T14:32:08Z"
}
```

### Override Reason Codes

Standardize override reasons to enable automated analysis:

| Code                    | Meaning                                    | Calibration Action                  |
|------------------------|--------------------------------------------|-------------------------------------|
| `INTENTIONAL_REORDER`  | Customer genuinely placed a second order    | Increase timestamp/delivery weight  |
| `AMENDED_PO`           | This is a revised version, not a duplicate  | Add revision detection heuristic    |
| `BLANKET_RELEASE`      | Release against blanket PO                  | Flag customer as blanket-PO type    |
| `SYSTEM_RETRY_VALID`   | Middleware retry was intentional/valid       | Reduce channel weight               |
| `DIFFERENT_SHIP_TO`    | Different destination = different order      | Increase ship-to weight             |
| `CONFIRMED_DUPLICATE`  | Agent was correct — this was a duplicate     | No change (positive reinforcement)  |
| `PARTIAL_OVERLAP`      | Some lines overlap but order is distinct     | Increase line-item Jaccard threshold|
| `OTHER`                | Free-text explanation required               | Manual review during analysis       |

> **V1 alignment note (per ADR-033):** these 8 codes have been adopted in `INTENT_REASON_TAGS["DUPLICATE_PO"]` from V1, so the calibration pipeline can consume V1 audit data directly when re-opened.

### Weekly Calibration Report

Generate weekly during Phase 2:

```
Week 3 Calibration Report — Walmart
────────────────────────────────────
Total detections:        142
  Agent correct:         118  (83.1%)
  False positives:        19  (13.4%)
  Missed duplicates:       5  (3.5%)

Top false positive pattern:
  INTENTIONAL_REORDER × 12 — same SKU set, same PO#, 7+ days apart
  → Recommendation: tighten timestamp window from 72h to 48h

Top missed duplicate pattern:
  Multi-channel resubmission × 3 — same PO via EDI then email within 2h
  → Recommendation: increase channel weight from 0.03 to 0.08

Proposed weight adjustments:
  timestamp:  0.12 → 0.14  (+0.02)
  channel:    0.03 → 0.08  (+0.05)
  line_items: 0.15 → 0.10  (-0.05)  (rebalance)
  po_number:  0.40 → 0.38  (-0.02)  (rebalance)
```

---

## Phase 3 — Continuous Tuning (Ongoing)

### Goal
Maintain detection accuracy as customer ordering patterns evolve over time.

### Automated Tuning Pipeline

```
Monthly Batch Job
    │
    ├── 1. Pull last 90 days of detection results + human resolutions
    │
    ├── 2. Compute override rate by signal
    │      "Which signals most often led to false positives?"
    │
    ├── 3. Re-fit logistic regression on labeled data
    │      (cumulative: Phase 1 historical + Phase 2/3 live data)
    │
    ├── 4. Compare new weights vs current weights
    │      If delta > 0.05 on any signal → flag for human review
    │      If delta ≤ 0.05 on all signals → auto-apply
    │
    ├── 5. Re-optimize thresholds on updated model
    │
    ├── 6. Generate calibration report → email to ops team
    │
    └── 7. Update customer-specific config (with audit trail)
```

### Autonomy Graduation

Based on sustained accuracy, the customer can graduate to higher autonomy:

| Metric Over 30-Day Window | Current | Graduate To |
|--------------------------|---------|-------------|
| Detection rate ≥ 98%, FP rate ≤ 2% | L2 | L3 (Act & Inform) |
| Detection rate ≥ 99%, FP rate ≤ 1%, zero buyer disputes | L3 | L4 (Full Autonomy) |
| FP rate > 5% OR buyer dispute | L3/L4 | Downgrade to L2 |
| Any missed duplicate causing financial impact > $10K | Any | Downgrade to L1 + emergency recalibration |

### Drift Detection

Monitor for pattern drift that signals recalibration is needed:

- **Override rate spikes** — If weekly override rate exceeds 15%, trigger immediate recalibration
- **New channel introduction** — Customer starts submitting via a new channel (e.g., adds API alongside EDI)
- **Seasonal patterns** — Holiday/promotional periods may have different duplicate patterns
- **Customer org changes** — Mergers, new subsidiaries, or procurement system changes
- **Score distribution shift** — If the median composite score for detected duplicates drops below 0.80, the model is losing discriminative power

---

## Configuration Override Hierarchy (Full)

Production systems must support this 5-level override hierarchy:

```
Level 1: Platform defaults (Section 2.1 of duplicate-po-product-spec.md)
  └── Level 2: Tenant defaults (CPG manufacturer's global settings)
      └── Level 3: Customer-tier overrides (Strategic / Standard / SMB)
          └── Level 4: Customer-specific overrides (Walmart gets its own weights)
              └── Level 5: Customer-channel overrides (Walmart EDI vs Walmart Portal)
```

Each level can override: `score_weights`, `thresholds`, `lookback_window_days`,
`timestamp_window_hours`, `amount_tolerance_pct`, `delivery_date_tolerance_days`,
and `autonomy_levels`.

Lower levels inherit from higher levels — only specify what you need to change.

> **V1 note:** the 5-level hierarchy itself is implemented in V1 per **ADR-030**; calibration *content* (the values that flow through it) is deferred to a future release per ADR-032.
