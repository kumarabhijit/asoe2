# Per-Intent Reason-Tag Curation — SME Discussion Questions

**Audience:** Domain SME sessions per Phase 28.3.
**Purpose:** Standardised question set the SME + Compliance reviewer answers per intent before the §5.2 engineering land. Use alongside `docs/templates/override_reason_tag_review_template.md`.
**Pre-read time:** ~15 minutes per intent.
**Session time:** ~90 minutes (45 sample-review + 30 question-walk + 15 sign-off).

---

## How to use this document

1. Pull the 100-row data sample per intent (template §1).
2. Read the cross-cutting questions in §A below — same for every intent.
3. Read the intent-specific questions in §B for the intent under review.
4. Capture answers into the per-intent template file.
5. Land the curated 4–6-entry list per the §3 template block.

The 12 intents from §28.3 are listed in §B in the recommended sequencing order (highest-volume first). **DUPLICATE_PO is already curated** (`constraints/specs.py::_DUPLICATE_PO_REASON_TAGS`, ADR-033 §A) — its 8 entries are the working example for the other intents.

---

## §A — Cross-cutting questions (asked once per intent)

These nine questions apply to every intent and surface the structural decisions that determine whether a curated list is auditable + ML-friendly.

### A1. Coverage

> Of the 100 sample rows, how many fit cleanly into one of the **6 global tags** (`customer_concession`, `contract_stale`, `data_error`, `policy_exception`, `agent_misclassification`, `other`)? Of the rest, how many cluster into a single intent-specific category, and how many genuinely need bespoke buckets?

Output: a per-intent `coverage_table` with `{global_tag, count}` for each row that fits a global tag, and the `bespoke_clusters` list for the rest.

### A2. Granularity floor

> Should we collapse rare reasons (<5% of sample) into `other`, or preserve them as named buckets for future calibration? At what frequency does a category earn its own slot?

Default per the review template: **5% floor** → smaller clusters fold into `other`. Override only when a rare bucket is SOX-bearing (Compliance flags it).

### A3. Audit-bearing fields per reason tag

> Does any candidate tag require additional structured fields the operator must capture (e.g., `contract_ref` when reason = `contract_stale`)? Are those fields already present on `resolution_data` / `enrichment_context`, or do we need a backend follow-up to capture them?

Output: a per-tag `required_fields:` list. If a field is missing today, add an audit-bearing-registry grandfather clause with deadline (CLAUDE.md Guardrail #6 path).

### A4. ML calibration relevance

> Which tags are signals the calibration loop should consume directly (per ADR-032)? Which are noise? Which are useful only when bucketed against a categorical observation (e.g., customer tier)?

Output: a `learning_signal:` annotation per tag — feeds `knowledge/compaction/override.template.md::reason_code_clusters`.

### A5. Cross-intent consistency

> Of the names already used by curated intents (today only `DUPLICATE_PO`'s 8 codes), which terms should be reused vs renamed for this intent? E.g., `BLANKET_RELEASE` exists under DUPLICATE_PO — does the same concept apply here under the same name, or differently?

Output: a `cross_intent_aliases:` map noting where this intent's tag matches another's. Reuse identical concepts; never overload meaning.

### A6. Threshold sensitivity

> Are any reason tags meaningfully different above vs below the four-eyes / cosign threshold ($10k)? E.g., `customer_concession` at $500 may be operations-approved while at $50k it may need finance sign-off.

Output: a `threshold_split_required:` flag. If true, the tag needs two variants (`*_LOW` / `*_HIGH`) or a separate `threshold_band:` field.

### A7. Time decay

> Do any reason tags have a freshness window where they're meaningful only within N days of the original event? E.g., `contract_stale` is meaningful only when a contract amendment landed within the last quarter.

Output: a `freshness_days:` number per tag (or `null` for no decay). Dashboards filter on this.

### A8. Customer-tier specificity

> Are any reasons valid only for specific customer tiers (Strategic / Mid-Market / Long-tail)? E.g., a `customer_concession` for a Strategic customer may carry different audit weight than for Long-tail.

Output: an `applicable_tiers:` list per tag (default `["all"]`). Compliance audit queries filter on this.

### A9. Anti-pattern check

> Is any candidate tag effectively saying "the agent was wrong" without giving Compliance a corrective signal? Those tags should be folded into `agent_misclassification` (the global tag exists for exactly this reason).

Output: confirm each retained tag IS NOT a synonym for `agent_misclassification`. If it is, drop it.

---

## §B — Per-intent question sets

Listed in the §28.3 sequencing order. Each block carries the questions specific to that intent's typical override patterns. **Always pair with the §A cross-cutting questions.**

### B1. CONTRACTUAL_CORRECTION (Recipe: `PriceAdjustmentRecipe`)

Pricing-discrepancy intent. Operator overrides typically reflect contract / promo / master-data fixes.

1. **Pricing source distinctions.** When the override reason is "the price was wrong", do we need separate tags for: contract-stale (price changed but SAP has the old one); promo-window (promo expired but PO predates expiry); SAP-master-data-error (the base price itself is wrong); customer-concession (one-time discount granted)?
2. **Direction.** Does the reason-tag need to capture whether the override moved the price *up* or *down*? Is "price moved up to match contract" different from "price moved down to honour promo"?
3. **Tolerance band.** When the deterministic recipe was within tolerance but the operator still overrode, is that a single `policy_exception`, or do we want a `band_review_overridden` distinction?
4. **Customer-driven vs ops-driven.** Are operations-team overrides (e.g., re-extract from SAP after a master-data fix) the same audit-class as customer-driven overrides (CSR honoured a buyer's request)?
5. **Re-extract path.** If the override is "the data was wrong, fixed it, re-extract", does the `data_error` tag suffice, or does Compliance want `data_error_reextracted` to distinguish from `data_error_grandfathered` (left as-is)?

### B2. CREDIT_BLOCK (Recipe: `CreditHoldReleaseRecipe`)

Credit-limit-driven holds.

1. **Source of the release.** Distinct tags for: finance-team approved (one-time exception), credit-line-adjusted (durable change), payment-received (block lifted naturally), customer-pre-pay-arranged?
2. **Risk acknowledgement.** When operations releases a credit-blocked order, is the audit signal best captured as a single `risk_accepted` tag, or do we need `risk_accepted_one_time` vs `risk_accepted_pattern_customer`?
3. **Exposure context.** Should the tag carry the exposure delta at override time (e.g., `released_with_$X_over_limit`)? Or is that a separate audit-bearing field?
4. **Customer-tier interplay.** Strategic customer credit-block releases vs Long-tail customer releases — same audit class? Or do we need a Strategic-specific tag for the higher-trust customer profile?
5. **Cosign threshold.** When the financial impact crosses the four-eyes threshold, the cosign flow already records a separate audit. Does that mean the credit-block reason-tag vocabulary doesn't need a `*_HIGH` variant?

### B3. MANUAL_ORDER_INTAKE (Recipe: `EmailOrderEntryRecipe` — formerly `EMAIL_ORDER_ENTRY`)

Email / phone / fax order extraction.

1. **Extraction failure modes.** Distinct tags for: `extraction_low_confidence` (LLM not sure); `field_missing` (PO format incomplete); `unsupported_format` (e.g., handwritten fax); `duplicate_manual_order` (same buyer, same PO sent twice over different channels)?
2. **Buyer intent verification.** When the operator confirms the buyer's intent (called the buyer / clarified by email), should that be `buyer_clarified` (positive signal) vs `buyer_unreachable` (negative signal)?
3. **Channel-specific signals.** Does email-channel override differ from phone-channel override (when phone lands)? E.g., voice-call note vs forwarded-email evidence — different audit classes?
4. **Auto-correct vs reject.** When the operator overrode an LLM auto-reject, was it because the LLM missed a valid PO format (`agent_misclassification`) or because the customer asked for a one-time format exception (`policy_exception`)? Audit query needs to distinguish.
5. **Multi-PO email.** When one email carries multiple POs and the operator manually splits them, is that a `multi_po_split` tag (positive process signal) or just `data_error` for the original mis-extraction?

### B4. EDI_MISMATCH (Recipe: `EdiMismatchRecipe`)

EDI 850 / 855 line-mismatch resolution. **Note:** ADR-034 sub-types this on `metadata.mismatch_sub_type` (`PRICE_MISMATCH` / `QTY_MISMATCH` / `LINE_NOT_FOUND` / etc.).

1. **Mismatch sub-type alignment.** Should the reason-tag vocabulary fan out per `mismatch_sub_type`, or do we keep a single intent-level vocabulary that callers correlate with `mismatch_sub_type` post-hoc?
2. **Partner-mapping issue.** Distinct tags for: `edi_mapping_error` (asoe2 partner-config wrong); `customer_edi_misconfigured` (customer's side); `sap_master_diff` (the SAP master no longer matches what was sent)?
3. **Tolerance vs mismatch.** When the discrepancy was within a tolerance band but still flagged, is the override a single `tolerance_band_accepted` tag or do we need to split per dimension (`price_tol_accepted` vs `qty_tol_accepted`)?
4. **Resubmit path.** When the operator triggers an EDI 855 resubmit instead of overriding, is there a `resubmitted_to_partner` reason or is that a separate workflow that doesn't need a reason-tag?
5. **PRICE_MISMATCH overlap.** Per `skills/loader.py:138`, EDI_MISMATCH with `mismatch_sub_type=PRICE_MISMATCH` loads the pricing-reconciliation skill — i.e., the override is functionally a CONTRACTUAL_CORRECTION. Should the reason-tags be aliased / shared with B1?

### B5. BACK_ORDER (Recipe: `BackOrderResolutionRecipe`)

Out-of-stock resolution.

1. **Resolution method.** Distinct tags for: `alternate_warehouse_approved`; `substitute_sku_approved`; `customer_accepts_delay`; `partial_fulfilment_accepted`; `cancellation_accepted`?
2. **Customer authority.** When operations decides on the resolution method without buyer confirmation (e.g., shipping from alternate warehouse silently), does the audit need a `buyer_unaware` flag? Or is the resolution-method tag sufficient?
3. **SLA breach correlation.** When the back-order resolution was driven by an SLA-breach trigger (vs proactive), should that be a separate reason-tag or a separate audit-bearing field?
4. **Cost-tier distinctions.** Substitute-SKU at customer's expense vs at company's expense — same tag (`substitute_sku_approved`) with a `cost_borne_by:` audit field, or two separate tags?
5. **Sub-pattern consistency.** Of the BackOrderResolutionRecipe outputs (`alternate_warehouse`, `substitute`, `delay`, `cancel`), do all four need parallel reason-tags, or is `cancellation_accepted` enough since the recipe itself produced the recommendation?

### B6. PRICE_HOLD_RELEASE (Recipe: `PriceHoldReleaseRecipe`)

Price-hold-flagged orders manually released.

1. **Authorisation source.** Distinct tags for: `operations_authorised` (ops manager); `finance_authorised` (finance signed off); `contract_verified` (legal-team confirmed contract terms)?
2. **Bulk vs single.** When the operator releases multiple price-hold records together (`mass_release` event), is that a single tag for each record or a different intent-level event entirely? Does the bulk action need its own reason-tag (`bulk_release_approved`) per record?
3. **Hold-source granularity.** Price holds come from multiple upstream signals (contract-band, promo-window-expiry, customer-flag, etc.). When the override-reason maps to which signal was wrong, does the tag need to call out the signal source, or is `policy_exception` sufficient?
4. **Re-hold pattern.** When a record is released, then re-held a week later (signal still firing), should the second override have a different reason-tag class indicating recurrence?
5. **Risk acknowledgement parity.** Same question as B2.2 — `risk_accepted_one_time` vs `risk_accepted_pattern_customer`?

### B7. OVER_MAX (Recipe: `OverMaxTrimRecipe`)

Order quantity exceeds customer max.

1. **Trim vs allow.** Distinct tags for `trim_applied` (operator approved trimming to max) vs `over_max_allowed` (operator approved shipping the over-max quantity)?
2. **Reason for over-max.** When the customer asked for the over-max, distinct tags for `customer_request` vs `seasonal_exception` vs `contract_pre_negotiated`?
3. **Customer max source.** When the customer max came from a stale config, is the audit signal best captured as `data_error` (global tag, registry-bearing) or a more specific `customer_max_stale` tag?
4. **Trim percent banding.** Does the audit need to distinguish trim percent (e.g., trimmed by 5% vs trimmed by 50%)? Or is the absolute quantity the audit-bearing field?

### B8. MIN_ORDER_QTY (Recipe: `MoqRoundUpRecipe`)

Order quantity below MOQ.

1. **Resolution method.** Distinct tags for `round_up_applied` (recipe brought qty up to MOQ); `customer_accepted_round_up` (buyer agreed); `partial_fulfilment_accepted` (shipped under-MOQ); `cancellation_accepted`?
2. **MOQ source.** When the MOQ came from a stale config, same question as B7.3 — bespoke tag or global `data_error`?
3. **Customer opt-out.** Customers with `restrict_round_up=true` show up as overrides when the recipe round-ups anyway. Distinct tag (`opt_out_violation_round_up`) or fold into `policy_exception`?

### B9. PALLET_CONFIG (Recipe: `PalletAlignmentRecipe`)

Pallet alignment / load planning issue.

1. **Resolution dimension.** Distinct tags for `tie_layer_overridden`; `pallet_height_overridden`; `freight_class_overridden`; `case_count_overridden`?
2. **Customer cost impact.** When the override changes who bears the freight delta, is that an audit-bearing field per record, or a separate tag (`customer_pays_diff` vs `seller_pays_diff`)?
3. **Carrier-specific overrides.** Different carriers have different pallet config rules; should the tag carry the carrier (`carrier_<name>_exception`) or is that a separate field?

### B10. DELIVERY_DELAY (Recipe: `DeliveryDelayRecipe`)

Delivery date discrepancy.

1. **Resolution method.** Distinct tags for `customer_accepts_new_date`; `expedite_approved` (and at whose cost?); `cancellation_accepted`; `partial_split_shipment`?
2. **Delay source.** Is the audit improved by distinguishing internal-process delay (`process_delay`) from carrier delay (`carrier_delay`) from customer-side delay (`customer_paperwork_delay`)?
3. **Threshold for tag-vs-no-tag.** When the delay is small (e.g., 1 day) and the operator just acknowledges it, do we want a `minor_delay_accepted` tag or skip the audit entry?

### B11. MASS_PRICING_ERROR (Recipe: none — RED-blocking intent)

Bulk price discrepancy. RED verdict; today's reviewer flow is admin-release.

1. **Admin-release reason granularity.** When an admin releases a MASS_PRICING_ERROR-flagged record, distinct tags for: `bulk_data_error_confirmed` (the prices were genuinely wrong, fixed in source); `bulk_promo_extension` (a promo was extended retroactively); `customer_pre_arranged_concession` (mass discount honoured); `false_positive` (the detection mis-flagged)?
2. **Bulk-action audit.** When admin releases N records in one bulk action, do they all carry the same reason-tag, or does each record's reason need to be assessed independently? Compliance preference.
3. **Re-extract path.** Same as B1.5 — `data_error_reextracted` vs `data_error_grandfathered`?

### B12. (Reserved — DUPLICATE_PO already curated)

DUPLICATE_PO carries 8 reason tags in `_DUPLICATE_PO_REASON_TAGS` (ADR-033 §A). Use them as the worked example for the §A cross-cutting questions:

| Tag | §A4 learning signal | §A6 threshold-split |
|---|---|---|
| `INTENTIONAL_REORDER` | yes (training: when reorder is genuine) | no |
| `AMENDED_PO` | yes (training: distinguish revision from new) | no |
| `BLANKET_RELEASE` | yes (training: blanket-PO recognition) | no |
| `SYSTEM_RETRY_VALID` | low (system noise) | no |
| `DIFFERENT_SHIP_TO` | yes (training: ship-to as differentiator) | no |
| `CONFIRMED_DUPLICATE` | yes (gold-label for duplicate detector) | no |
| `PARTIAL_OVERLAP` | yes (training: line-overlap policy) | no |
| `OTHER` | escape hatch | no |

The DUPLICATE_PO list satisfies §A1 (8 buckets), §A2 (each clearly named), §A3 (no extra fields required beyond what `matched_po_details` already carries), §A4 (every tag has a learning signal), §A5 (no aliasing — these are bespoke), §A6 (no threshold split), §A7 (no time decay), §A8 (all tiers), §A9 (no synonyms for `agent_misclassification`).

---

## §C — Cross-intent decisions to ratify

After all 11 sessions, two final decisions land:

### C1. Aliasing

> Are there reason tags appearing under multiple intents that should be aliased to a single canonical name? E.g., `customer_concession` under CONTRACTUAL_CORRECTION + CREDIT_BLOCK — same audit class? Or is each intent's `customer_concession` semantically distinct?

The §A5 outputs across intents drive this decision. Default: alias when the audit signal is identical; bespoke when intent context changes the meaning.

### C2. Engineering land sequencing (Phase 5.2)

> Do all 11 lists (after DUPLICATE_PO) land in `INTENT_REASON_TAGS` together, or in batches as sessions complete?

Recommended: batch of 3 (B1–B3 = highest-volume), validate the §5.3 grandfather-validator path, then ship the rest incrementally. Mirrors the §28.3 "land §5.2 wiring after sessions 1–3" recommendation.

### C3. ML calibration handoff

> Once curated tuples are in production, what cadence does the ML team consume them? Weekly clustering report? Real-time streaming?

The `knowledge/compaction/override.template.md::reason_code_clusters` learning signal is the formal handoff. The ML team's specific consumption pattern is their call; engineering ships the data, not the dashboard.

---

## Output of each session

Per the existing template at `docs/templates/override_reason_tag_review_template.md`:

1. A new file under `docs/workshops/reason-tags/<intent>-<date>.md` filled with the answers above.
2. A 4–6-entry curated list, ready for `constraints/specs.py::INTENT_REASON_TAGS[<INTENT>]`.
3. Compliance sign-off block signed.

After 3 sessions the §5.2 engineering land happens; remaining intents add their lists incrementally (see §C2).

---

*Authored 2026-05-09 alongside the Phase 28 next-session pickup block. Read alongside the existing review template (`docs/templates/override_reason_tag_review_template.md`) and ADR-033.*
