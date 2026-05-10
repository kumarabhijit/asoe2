# CPG SME Panel — Per-Intent Reason-Tag Curation Responses

**Date:** 2026-05-10
**Source questions:** [`docs/workshops/per-intent-reason-tag-questions.md`](../per-intent-reason-tag-questions.md) (commit `a9d4718`)
**Format:** Consolidated single-session output covering all 11 intents (B1–B11). Each block contains §A cross-cutting answers, §B intent-specific answers, the curated 4–6-entry list ready for `constraints/specs.py::INTENT_REASON_TAGS[<INTENT>]`, and a Compliance sign-off block.
**Sequel deliverables:** Per the source doc §"Output of each session", the curated lists in this file are ready for §5.2 engineering land batched per §C2 below.

---

## Panel composition

| SME | Role | Signing authority |
|---|---|---|
| Maria Alvarez | Director, Order-to-Cash Operations (15 yrs CPG; ex-P&G, ex-Mondelez) | Override-decision domain owner |
| David Chen | Sr. Manager, Pricing & Trade (12 yrs; ex-Coca-Cola, ex-General Mills) | Contract / promo / price-list authority |
| Priya Subramanian | Credit Risk Manager (10 yrs; ex-Unilever) | Credit-block release authority |
| Tom O'Brien | EDI / Customer Integration Lead (18 yrs; ex-Kraft Heinz, ex-Walmart-side) | Partner-mapping authority |
| James Whitfield | Demand Planning / CSR Lead (11 yrs; ex-Nestlé) | Back-order / delivery / MOQ authority |
| Sandra Park, CPA | Compliance & SOX Lead (14 yrs; ex-Big 4 → CPG controllership) | Audit-bearing field authority |
| Karen Wills | Finance / FP&A representative | Cosign-threshold authority |
| Dr. Lin Ma | ML / Data Science Lead (in-house ASOE team) | Calibration-signal consumer |

> **Note on simulated panel.** This document is the output of a single consolidated working session that substitutes for the eleven 90-minute SME meetings prescribed in §28.3. The curated lists below are intended to **seed** the `INTENT_REASON_TAGS` table; ratification by the named human SMEs at the next live workshop remains a hard gate before the §5.2 engineering land. Treat each list as a strong starting proposal, not a final compliance sign-off.

---

## §A — Framework defaults the panel ratified once for all intents

These are the cross-cutting decisions the panel landed before walking each intent. Per-intent overrides are noted in each B-block.

| § | Question | Panel default |
|---|---|---|
| A1 | Coverage against the 6 global tags | Target 50–70% global-tag coverage per intent. Below 50%, the intent's domain is too bespoke for global-tag dominance and bespoke buckets dominate (CPG fact pattern). |
| A2 | Granularity floor | 5% sample-frequency floor holds. Override only when Sandra Park (Compliance) flags the bucket as SOX-bearing — explicitly noted per intent below. |
| A3 | Audit-bearing fields | Captured per tag in each B-block as `required_fields:`. Where a field is missing today, follow CLAUDE.md Guardrail #6 grandfather path (registry entry + deadline). |
| A4 | ML calibration relevance | Three-way: `gold` (direct training signal), `categorical` (only useful when bucketed against tier/exposure/etc.), `noise` (low signal, retained for audit only). Tagged per entry. |
| A5 | Cross-intent consistency | Alias when audit signal is identical across intents; bespoke when intent context changes meaning. Aliasing decisions consolidated in §C1. |
| A6 | Threshold sensitivity | Use `_LOW`/`_HIGH` variants only where the operational decision differs (e.g., `CUSTOMER_CONCESSION` flips from ops-approved to finance-cosign at $10k). For tags where the cosign workflow already creates a separate audit trail, **no** threshold split is needed in the tag (Karen Wills' position; ratified). |
| A7 | Time decay | `freshness_days:` field per tag where meaningful. Default `null`. |
| A8 | Customer-tier specificity | `applicable_tiers: [strategic, mid_market, long_tail]` default. Override per tag where Sandra Park requires tier-restricted dashboards. |
| A9 | Anti-pattern check | A tag is dropped if it secretly says "the agent was wrong" with no corrective signal — fold to `agent_misclassification`. Each kept tag justified per intent. |

**Cross-cutting ratifications:**
- Direction (price up vs down), buyer-confirmed (yes/no), exposure delta, freight-cost-borne-by, channel, carrier, hold-source, delay-source — all captured as **audit-bearing fields**, never as tag fan-out. Vocabulary economy is a hard panel preference (Sandra Park: "every redundant tag is a future audit-query branch").
- The `OTHER` escape hatch is mandatory in every list. Required field on `OTHER`: `free_text_reason` (≥10 chars) for downstream clustering.

---

## §B — Per-intent curations

### B1. CONTRACTUAL_CORRECTION

**§A intent-specific:**
- **A1 Coverage** — ~62/100 fit global tags (predominantly `contract_stale`, `data_error`, `customer_concession`); ~38 cluster around price-direction nuance and tolerance-band overrides. Bespoke buckets justified.
- **A2 Granularity** — 5% floor holds, with one override: `BAND_REVIEW_OVERRIDDEN` runs ~3% of sample but Sandra Park flags it SOX-bearing (within-band override is exactly what auditors interrogate).
- **A3 Required fields** — see per-tag list below.
- **A4 ML relevance** — `CONTRACT_STALE_PRICE`, `SAP_MASTER_PRICE_ERROR`, `PROMO_WINDOW_EXPIRED` are gold signals; `CUSTOMER_CONCESSION` is categorical (against tier + exposure); `BAND_REVIEW_OVERRIDDEN` is noise (audit-only).
- **A5 Aliasing** — `CUSTOMER_CONCESSION` aliases across B2 (CREDIT_BLOCK) and the EDI_MISMATCH/PRICE_MISMATCH overlap (B4.5). Other tags bespoke to pricing.
- **A6 Threshold split** — Yes for `CUSTOMER_CONCESSION` (`_LOW` < $10k, `_HIGH` ≥ $10k). No others.
- **A7 Time decay** — `CONTRACT_STALE_PRICE` meaningful within 90 days of contract amendment; `PROMO_WINDOW_EXPIRED` within ±14 days of promo expiry.
- **A8 Tier** — All tiers; `CUSTOMER_CONCESSION_HIGH` for Long-tail flagged for higher audit weight (anomaly).
- **A9 Anti-pattern** — `BAND_REVIEW_OVERRIDDEN` interrogated; kept because it captures legitimate ops judgment overriding an in-band recipe output (not "agent wrong"; "ops chose differently").

**§B1 specific answers:**
1. **Pricing source distinctions** — Yes, separate tags per source (contract-stale, promo-window, SAP-master, concession). Operator picks one; otherwise the audit signal collapses.
2. **Direction (up/down)** — Captured as audit-bearing field `price_delta_direction: up|down`, not tag fan-out (would double the vocabulary).
3. **Tolerance band** — Distinct `BAND_REVIEW_OVERRIDDEN` bucket, per A9 reasoning.
4. **Customer-driven vs ops-driven** — Same audit class; captured via `override_initiator: ops|csr|finance` field.
5. **Re-extract path** — Captured via per-tag audit field `data_error_resolution: reextracted|grandfathered`, not a tag fan-out. Same pattern reused in B4 and B11 (§C1).

**Curated list (6 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `CONTRACT_STALE_PRICE` | gold | `contract_ref`, `contract_amend_date`, `data_error_resolution` | 90 | no |
| `PROMO_WINDOW_EXPIRED` | gold | `promo_id`, `promo_window_start`, `promo_window_end` | 14 | no |
| `SAP_MASTER_PRICE_ERROR` | gold | `master_data_field`, `data_error_resolution` | null | no |
| `CUSTOMER_CONCESSION` | categorical | `concession_amount_usd`, `concession_authorized_by`, `tier` | null | yes (`_LOW`/`_HIGH` at $10k) |
| `BAND_REVIEW_OVERRIDDEN` | noise (audit-only) | `tolerance_band_pct`, `band_decision_rationale` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Sandra Park Compliance sign-off (B1):** ☑ — pending live ratification. `BAND_REVIEW_OVERRIDDEN` retention is the SOX-floor exception; document the rationale on the registry entry.

---

### B2. CREDIT_BLOCK

**§A intent-specific:**
- **A1 Coverage** — ~40/100 to global tags; ~60 to bespoke. Credit domain has its own compliance vocabulary (Priya: "finance audit doesn't speak in `customer_concession` here").
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `exposure_delta_usd` mandatory on every release (Sandra Park hard requirement).
- **A4 ML relevance** — `CREDIT_LINE_ADJUSTED` and `CUSTOMER_PREPAY_ARRANGED` are gold (durable state changes); `FINANCE_RELEASED_ONE_TIME` and `RISK_ACCEPTED_OPS` are categorical (against customer tier + payment history).
- **A5 Aliasing** — `RISK_ACCEPTED_OPS` aliases with the analogous concept in B6 (PRICE_HOLD_RELEASE). See §C1.
- **A6 Threshold split** — No. The cosign workflow already records a separate audit trail above $10k; tag vocab unchanged. (Karen Wills ratified.)
- **A7 Time decay** — None.
- **A8 Tier** — Strategic-tier exposures flagged via `tier` field on the release record; same vocabulary, different audit query.
- **A9 Anti-pattern** — None retained.

**§B2 specific answers:**
1. **Source of release** — Three distinct tags (finance / line-adjusted / prepay-arranged) plus the ops-overrode-risk path. Natural payment-clear is auto-resolved (no override event), so no tag.
2. **Risk acknowledgement** — Single `RISK_ACCEPTED_OPS` tag with audit field `pattern_customer: bool`. Two-tag fan-out rejected for vocabulary economy.
3. **Exposure context** — Audit-bearing field `exposure_delta_usd`, not a tag suffix.
4. **Customer-tier interplay** — Same tag; `tier` audit field. Compliance dashboard slices on tier post-hoc.
5. **Cosign threshold** — Confirmed: cosign flow's separate audit removes the need for a `_HIGH` variant.

**Curated list (5 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `FINANCE_RELEASED_ONE_TIME` | categorical | `finance_approver_id`, `exposure_delta_usd`, `tier` | null | no |
| `CREDIT_LINE_ADJUSTED` | gold | `prior_limit`, `new_limit`, `adjusted_by`, `effective_date` | null | no |
| `CUSTOMER_PREPAY_ARRANGED` | gold | `prepay_amount`, `prepay_method`, `prepay_ref` | null | no |
| `RISK_ACCEPTED_OPS` | categorical | `exposure_delta_usd`, `pattern_customer`, `tier` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Priya Subramanian Credit sign-off (B2):** ☑ — pending live ratification. Hard requirement: `exposure_delta_usd` non-null at write time; reject the override otherwise.

---

### B3. MANUAL_ORDER_INTAKE

**§A intent-specific:**
- **A1 Coverage** — ~35/100 to global tags (mostly `data_error`, `agent_misclassification`); ~65 bespoke. Intake-failure modes don't map to global vocab cleanly.
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `extraction_confidence_score` on `EXTRACTION_LOW_CONFIDENCE`; `clarification_evidence_ref` on `FIELD_MISSING_BUYER_CLARIFIED`.
- **A4 ML relevance** — `EXTRACTION_LOW_CONFIDENCE` is gold (LLM training); `MULTI_PO_SPLIT` is gold (process-quality signal); `FIELD_MISSING_*` and `UNSUPPORTED_FORMAT` are categorical.
- **A5 Aliasing** — None to other intents.
- **A6 Threshold split** — No.
- **A7 Time decay** — None.
- **A8 Tier** — All tiers.
- **A9 Anti-pattern** — Considered a `manual_corrected_extraction` tag; rejected — would be a synonym for `agent_misclassification`. Folded.

**§B3 specific answers:**
1. **Extraction failure modes** — Four kept (low-confidence, field-missing-clarified, field-missing-unreachable, unsupported-format). `duplicate_manual_order` belongs in DUPLICATE_PO, not here.
2. **Buyer intent verification** — Yes, `_BUYER_CLARIFIED` vs `_BUYER_UNREACHABLE` split is materially different (one is positive evidence; the other is best-guess). Maria Alvarez: "those two are not the same audit class".
3. **Channel-specific signals** — Audit-bearing field `channel: email|phone|fax`; not tag fan-out.
4. **Auto-correct vs reject** — Use existing `EXTRACTION_LOW_CONFIDENCE` for the LLM-missed-valid-PO case; the customer-format-exception case lands as global `policy_exception`. Compliance distinguishes via the global `agent_misclassification` flag.
5. **Multi-PO email** — `MULTI_PO_SPLIT` retained as a positive-process tag.

**Curated list (6 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `EXTRACTION_LOW_CONFIDENCE` | gold | `extraction_confidence_score`, `model_version` | null | no |
| `FIELD_MISSING_BUYER_CLARIFIED` | categorical | `clarification_evidence_ref`, `clarified_by`, `channel` | null | no |
| `FIELD_MISSING_BUYER_UNREACHABLE` | categorical | `attempts_made`, `best_guess_field`, `channel` | null | no |
| `UNSUPPORTED_FORMAT` | categorical | `format_descriptor`, `channel` | null | no |
| `MULTI_PO_SPLIT` | gold | `parent_message_id`, `child_po_ids[]` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Maria Alvarez Operations sign-off (B3):** ☑ — pending live ratification.

---

### B4. EDI_MISMATCH

**§A intent-specific:**
- **A1 Coverage** — ~50/100 to global tags (`data_error` dominates); ~50 bespoke (partner-side / SAP-side).
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `partner_id`, `edi_doc_type` (850/855), `mismatch_sub_type` (mandatory; already on the intent metadata).
- **A4 ML relevance** — `EDI_PARTNER_MAPPING_ERROR`, `CUSTOMER_EDI_MISCONFIGURED`, `SAP_MASTER_DIFF` are gold (drive partner-config governance); `TOLERANCE_BAND_ACCEPTED` is categorical; `RESUBMITTED_TO_PARTNER` is process-quality (gold).
- **A5 Aliasing** — **Critical**: `mismatch_sub_type=PRICE_MISMATCH` aliases to B1 vocabulary (see §C1).
- **A6 Threshold split** — No.
- **A7 Time decay** — `SAP_MASTER_DIFF` meaningful within 30 days of master-data refresh.
- **A8 Tier** — All tiers; partner-specific governance does its own slicing on `partner_id`.
- **A9 Anti-pattern** — None retained.

**§B4 specific answers:**
1. **Mismatch sub-type alignment** — Single intent-level vocabulary; correlate with `mismatch_sub_type` post-hoc. Vocabulary fan-out per sub-type rejected (would 4×-explode the list).
2. **Partner-mapping issue** — Three distinct sources kept (asoe2 partner-config, customer-side, SAP-master-diff).
3. **Tolerance vs mismatch** — Single `TOLERANCE_BAND_ACCEPTED` with audit field `dimension: price|qty|date`. Tom O'Brien: "the dimension is structured data, not a vocabulary concern".
4. **Resubmit path** — Yes, `RESUBMITTED_TO_PARTNER` retained; the audit cares about resubmit-vs-override outcomes for partner-relationship reporting.
5. **PRICE_MISMATCH overlap** — **Aliased**: when `mismatch_sub_type=PRICE_MISMATCH`, the operator picks from B1's vocabulary; the validator rejects B4-vocabulary tags in this case. See §C1 for engineering-land detail.

**Curated list (6 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `EDI_PARTNER_MAPPING_ERROR` | gold | `partner_id`, `mapping_field`, `data_error_resolution` | null | no |
| `CUSTOMER_EDI_MISCONFIGURED` | gold | `partner_id`, `edi_segment`, `customer_acknowledged: bool` | null | no |
| `SAP_MASTER_DIFF` | gold | `master_data_field`, `data_error_resolution` | 30 | no |
| `TOLERANCE_BAND_ACCEPTED` | categorical | `dimension`, `tolerance_band_pct` | null | no |
| `RESUBMITTED_TO_PARTNER` | gold | `resubmit_doc_id`, `resubmit_timestamp` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Constraint:** when `metadata.mismatch_sub_type == "PRICE_MISMATCH"`, the validator MUST require the operator to select from B1's vocabulary instead. Engineering surface in `constraints/specs.py`: a sub-type-aware validator that reads `INTENT_REASON_TAGS["CONTRACTUAL_CORRECTION"]` for that path. See §C1.

**Tom O'Brien EDI sign-off (B4):** ☑ — pending live ratification. Sandra Park co-signs the PRICE_MISMATCH alias rule (Compliance dual-key requirement).

---

### B5. BACK_ORDER

**§A intent-specific:**
- **A1 Coverage** — ~25/100 to global tags; ~75 bespoke. Resolution-method vocabulary doesn't map to global tags.
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `buyer_unaware: bool` mandatory (Sandra Park flagged for customer-comms compliance); `cost_borne_by` mandatory on `SUBSTITUTE_SKU_APPROVED`.
- **A4 ML relevance** — All four resolution-method tags are gold (drive substitution / cancel-rate models); `OTHER` is escape.
- **A5 Aliasing** — `CANCELLATION_ACCEPTED` and `PARTIAL_FULFILMENT_ACCEPTED` recur in B8 (MIN_ORDER_QTY) and B10 (DELIVERY_DELAY) — same audit class, ALIAS. See §C1.
- **A6 Threshold split** — No.
- **A7 Time decay** — None.
- **A8 Tier** — All tiers; Strategic-tier `buyer_unaware=true` records flagged for higher audit weight (CSR escalation policy).
- **A9 Anti-pattern** — None.

**§B5 specific answers:**
1. **Resolution method** — Five distinct methods kept (matches recipe outputs 1:1; clean for ML).
2. **Customer authority** — `buyer_unaware: bool` audit-bearing field on every override (especially `ALTERNATE_WAREHOUSE_APPROVED` and `SUBSTITUTE_SKU_APPROVED`). Customer-comms compliance hard requirement.
3. **SLA breach correlation** — Captured via separate `trigger_source: sla_breach|proactive` field, not a tag.
4. **Cost-tier distinctions** — `cost_borne_by: customer|seller` audit field on `SUBSTITUTE_SKU_APPROVED`.
5. **Sub-pattern consistency** — Yes, all four parallel tags retained.

**Curated list (6 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `ALTERNATE_WAREHOUSE_APPROVED` | gold | `warehouse_id`, `buyer_unaware`, `trigger_source` | null | no |
| `SUBSTITUTE_SKU_APPROVED` | gold | `original_sku`, `substitute_sku`, `cost_borne_by`, `buyer_unaware` | null | no |
| `CUSTOMER_ACCEPTS_DELAY` | gold | `new_promised_date`, `buyer_confirmed_via` | null | no |
| `PARTIAL_FULFILMENT_ACCEPTED` | gold | `original_qty`, `fulfilled_qty`, `buyer_confirmed_via` | null | no |
| `CANCELLATION_ACCEPTED` | gold | `cancel_initiator`, `buyer_confirmed_via` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**James Whitfield Demand-Planning sign-off (B5):** ☑ — pending live ratification. `buyer_unaware=true` records require dotted-line review with CSR director monthly.

---

### B6. PRICE_HOLD_RELEASE

**§A intent-specific:**
- **A1 Coverage** — ~30/100 to global tags (mostly `policy_exception`); ~70 bespoke (authorisation source dominates).
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `authorising_party_id`, `hold_source` (already on the inbound record).
- **A4 ML relevance** — All authorisation-source tags categorical (against authoriser tenure / customer tier). `BULK_RELEASE_APPROVED` gold (process-quality).
- **A5 Aliasing** — `RISK_ACCEPTED_OPS` aliases to B2; same canonical name.
- **A6 Threshold split** — No (cosign captures it separately).
- **A7 Time decay** — None.
- **A8 Tier** — All tiers.
- **A9 Anti-pattern** — None.

**§B6 specific answers:**
1. **Authorisation source** — Three sources kept (operations / finance / contract-verified).
2. **Bulk vs single** — Per-record tag with `bulk_action_id` audit field. Not a separate intent. Compliance audits per-record but rolls up via `bulk_action_id`.
3. **Hold-source granularity** — Captured via existing `hold_source` field on the inbound record; tag stays at authorisation level. Avoid double-encoding.
4. **Re-hold pattern** — Captured via `prior_release_count` audit field, not a separate tag. Recurrence detection is downstream analytics, not vocabulary.
5. **Risk acknowledgement parity** — Same `RISK_ACCEPTED_OPS` + `pattern_customer: bool` pattern as B2.

**Curated list (5 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `OPERATIONS_AUTHORISED` | categorical | `authorising_party_id`, `bulk_action_id`, `prior_release_count` | null | no |
| `FINANCE_AUTHORISED` | categorical | `finance_approver_id`, `bulk_action_id` | null | no |
| `CONTRACT_VERIFIED` | gold | `contract_ref`, `verified_by` | null | no |
| `BULK_RELEASE_APPROVED` | gold | `bulk_action_id`, `record_count_in_batch` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Note:** `BULK_RELEASE_APPROVED` is appended to the per-record reason when the release is part of a mass-release event; it can co-exist with one of the three authorisation tags (compound annotation supported in `resolution_data`).

**Karen Wills Finance sign-off (B6):** ☑ — pending live ratification.

---

### B7. OVER_MAX

**§A intent-specific:**
- **A1 Coverage** — ~40/100 to global tags; ~60 bespoke (resolution method + max-source).
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `trim_pct` on `TRIM_APPLIED`; `event_ref` on `OVER_MAX_ALLOWED_SEASONAL`.
- **A4 ML relevance** — All gold except `OTHER`.
- **A5 Aliasing** — None.
- **A6 Threshold split** — No.
- **A7 Time decay** — `OVER_MAX_ALLOWED_SEASONAL` meaningful within event window (carried via `event_ref`).
- **A8 Tier** — All tiers.
- **A9 Anti-pattern** — None.

**§B7 specific answers:**
1. **Trim vs allow** — Yes, distinct tags.
2. **Reason for over-max** — Two retained (customer-request, seasonal). `contract_pre_negotiated` cases re-classify as B1 (CONTRACTUAL_CORRECTION) — wrong intent.
3. **Customer max source** — Bespoke `CUSTOMER_MAX_STALE` (NOT global `data_error`). Sandra Park: "customer-config governance reporting needs the over-max-specific signal; folding to `data_error` loses it".
4. **Trim percent banding** — `trim_pct` audit field, not banded into the tag.

**Curated list (5 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `TRIM_APPLIED` | gold | `original_qty`, `trimmed_qty`, `trim_pct` | null | no |
| `OVER_MAX_ALLOWED_CUSTOMER_REQUEST` | gold | `original_qty`, `approved_qty`, `customer_request_ref` | null | no |
| `OVER_MAX_ALLOWED_SEASONAL` | gold | `original_qty`, `approved_qty`, `event_ref` | event-bound | no |
| `CUSTOMER_MAX_STALE` | gold | `customer_max_field`, `last_config_update` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Maria Alvarez sign-off (B7):** ☑ — pending live ratification.

---

### B8. MIN_ORDER_QTY

**§A intent-specific:**
- **A1 Coverage** — ~55/100 to global tags (`data_error`, `policy_exception`); ~45 bespoke.
- **A2 Granularity** — 5% floor holds. **`OPT_OUT_VIOLATION_ROUND_UP` runs ~2% but Sandra Park flags SOX-bearing** (guardrail-violation event).
- **A3 Required fields** — `customer_opt_out_flag` on `OPT_OUT_VIOLATION_ROUND_UP`.
- **A4 ML relevance** — `ROUND_UP_APPLIED` categorical (against tier); `OPT_OUT_VIOLATION_ROUND_UP` gold (defect-detection signal); others gold.
- **A5 Aliasing** — `PARTIAL_FULFILMENT_ACCEPTED` and `CANCELLATION_ACCEPTED` alias to B5 (BACK_ORDER), same canonical names.
- **A6 Threshold split** — No.
- **A7 Time decay** — None.
- **A8 Tier** — All tiers.
- **A9 Anti-pattern** — None.

**§B8 specific answers:**
1. **Resolution method** — Three resolution tags + opt-out-violation. `customer_accepted_round_up` collapsed into `ROUND_UP_APPLIED` with `buyer_confirmed: bool` field (vocabulary economy).
2. **MOQ source** — Use **global `data_error`** with a `data_error_resolution` field (asymmetric with B7 by design — MOQ is brand-level config and doesn't need a per-customer-MOQ-stale dashboard). Sandra Park ratified the asymmetry.
3. **Customer opt-out** — Bespoke `OPT_OUT_VIOLATION_ROUND_UP` (NOT folded into `policy_exception`). It's a guardrail violation; auditors need it visible.

**Curated list (5 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `ROUND_UP_APPLIED` | categorical | `original_qty`, `round_up_qty`, `buyer_confirmed`, `tier` | null | no |
| `PARTIAL_FULFILMENT_ACCEPTED` | gold | `original_qty`, `fulfilled_qty`, `buyer_confirmed_via` | null | no |
| `CANCELLATION_ACCEPTED` | gold | `cancel_initiator`, `buyer_confirmed_via` | null | no |
| `OPT_OUT_VIOLATION_ROUND_UP` | gold | `customer_opt_out_flag`, `override_initiator`, `escalation_ref` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Sandra Park sign-off (B8):** ☑ — pending live ratification. SOX-floor exception logged for `OPT_OUT_VIOLATION_ROUND_UP`.

---

### B9. PALLET_CONFIG

**§A intent-specific:**
- **A1 Coverage** — ~20/100 to global tags; ~80 bespoke (dimension-driven).
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `freight_delta_borne_by` on every override; `carrier_id` mandatory.
- **A4 ML relevance** — All four dimension tags are gold (Lin Ma: "load-planning model retraining specifically wants per-dimension override frequency").
- **A5 Aliasing** — None.
- **A6 Threshold split** — No.
- **A7 Time decay** — None.
- **A8 Tier** — All tiers.
- **A9 Anti-pattern** — None.

**§B9 specific answers:**
1. **Resolution dimension** — Four distinct tags kept (tie / height / freight-class / case-count); ML team's request.
2. **Customer cost impact** — `freight_delta_borne_by: customer|seller` audit-bearing field; not tag fan-out.
3. **Carrier-specific overrides** — `carrier_id` audit field; not in tag (avoids tag-explosion across 50+ carriers).

**Curated list (5 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `TIE_LAYER_OVERRIDDEN` | gold | `original_tie`, `override_tie`, `freight_delta_borne_by`, `carrier_id` | null | no |
| `PALLET_HEIGHT_OVERRIDDEN` | gold | `original_height`, `override_height`, `freight_delta_borne_by`, `carrier_id` | null | no |
| `FREIGHT_CLASS_OVERRIDDEN` | gold | `original_class`, `override_class`, `freight_delta_borne_by`, `carrier_id` | null | no |
| `CASE_COUNT_OVERRIDDEN` | gold | `original_count`, `override_count`, `freight_delta_borne_by`, `carrier_id` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Lin Ma ML sign-off (B9):** ☑ — pending live ratification. Per-dimension granularity is a calibration must.

---

### B10. DELIVERY_DELAY

**§A intent-specific:**
- **A1 Coverage** — ~30/100 to global tags; ~70 bespoke.
- **A2 Granularity** — 5% floor holds.
- **A3 Required fields** — `expedite_cost_borne_by` on `EXPEDITE_APPROVED`; `delay_source` (already on inbound record).
- **A4 ML relevance** — `CUSTOMER_ACCEPTS_NEW_DATE` and `EXPEDITE_APPROVED` gold; others gold.
- **A5 Aliasing** — `CANCELLATION_ACCEPTED` aliases to B5 / B8.
- **A6 Threshold split** — No.
- **A7 Time decay** — None.
- **A8 Tier** — Strategic-tier delays carry higher audit weight; same tags + `tier` field.
- **A9 Anti-pattern** — Considered `minor_delay_accepted`; **rejected** because it would encourage skipping audit entries (Sandra Park: "we don't ship vocabulary that incentivises a-b-c skipping").

**§B10 specific answers:**
1. **Resolution method** — Four kept.
2. **Delay source** — Captured via existing `delay_source` field on inbound; tag stays at resolution level.
3. **Threshold for tag-vs-no-tag** — No `minor_delay_accepted`; if the override is logged, it lands as `CUSTOMER_ACCEPTS_NEW_DATE`.

**Curated list (5 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `CUSTOMER_ACCEPTS_NEW_DATE` | gold | `original_date`, `new_promised_date`, `buyer_confirmed_via` | null | no |
| `EXPEDITE_APPROVED` | gold | `expedite_method`, `expedite_cost_borne_by`, `cost_estimate_usd` | null | no |
| `CANCELLATION_ACCEPTED` | gold | `cancel_initiator`, `buyer_confirmed_via` | null | no |
| `PARTIAL_SPLIT_SHIPMENT` | gold | `split_count`, `split_dates[]`, `buyer_confirmed_via` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**James Whitfield sign-off (B10):** ☑ — pending live ratification.

---

### B11. MASS_PRICING_ERROR

**§A intent-specific:**
- **A1 Coverage** — ~40/100 to global tags; ~60 bespoke.
- **A2 Granularity** — 5% floor holds; **all four named buckets are SOX-bearing per Sandra Park** (admin-release events are top-tier audit; no folding).
- **A3 Required fields** — `bulk_action_id` mandatory; `data_error_resolution` on `BULK_DATA_ERROR_CONFIRMED`.
- **A4 ML relevance** — `BULK_DATA_ERROR_CONFIRMED` and `BULK_PROMO_EXTENSION` gold; `MASS_CONCESSION_HONOURED` categorical (against tier); **`FALSE_POSITIVE` gold + real-time stream** to detector retraining (Lin Ma's request).
- **A5 Aliasing** — `MASS_CONCESSION_HONOURED` ≠ B1 `CUSTOMER_CONCESSION` (semantically distinct: per-line vs bulk-event).
- **A6 Threshold split** — No (admin-release IS the threshold flow).
- **A7 Time decay** — None.
- **A8 Tier** — Mixed; `MASS_CONCESSION_HONOURED` for Long-tail bulk flagged for higher audit weight.
- **A9 Anti-pattern** — None.

**§B11 specific answers:**
1. **Admin-release reason granularity** — Four named buckets (data-error, promo-extension, mass-concession, false-positive). All SOX-bearing.
2. **Bulk-action audit** — Each record carries its own reason-tag (independent assessment). `bulk_action_id` rolls up the batch. Compliance preference: per-record granularity.
3. **Re-extract path** — `data_error_resolution: reextracted|grandfathered` audit field on `BULK_DATA_ERROR_CONFIRMED`.

**Curated list (5 entries):**

| Tag | learning_signal | required_fields | freshness_days | threshold_split |
|---|---|---|---|---|
| `BULK_DATA_ERROR_CONFIRMED` | gold | `bulk_action_id`, `data_error_resolution`, `source_system` | null | no |
| `BULK_PROMO_EXTENSION` | gold | `bulk_action_id`, `promo_id`, `extension_window_days` | null | no |
| `MASS_CONCESSION_HONOURED` | categorical | `bulk_action_id`, `concession_total_usd`, `pre_arrangement_ref` | null | no |
| `FALSE_POSITIVE` | gold (real-time stream) | `bulk_action_id`, `detector_version`, `disagreement_evidence` | null | no |
| `OTHER` | escape hatch | `free_text_reason` | null | no |

**Sandra Park sign-off (B11):** ☑ — pending live ratification. All four named buckets SOX-mandatory; no folding to `OTHER`.

---

## §C — Cross-intent ratifications

### C1. Aliasing decisions

| Concept | Where it appears | Decision | Rationale |
|---|---|---|---|
| `CUSTOMER_CONCESSION` | B1 (per-line) vs `MASS_CONCESSION_HONOURED` B11 (bulk) | **Distinct** — keep separate | Per-line vs bulk-event are not the same audit class |
| `RISK_ACCEPTED_OPS` | B2 (CREDIT_BLOCK), B6 (PRICE_HOLD_RELEASE) | **Alias to single canonical name** | Same audit signal: ops accepting risk against a deterministic flag |
| `data_error_resolution: reextracted\|grandfathered` | B1, B4, B7, B11 | **Shared audit field** (not a tag) | Pattern reused across all data-error-driven intents |
| `BLANKET_RELEASE` | DUPLICATE_PO only | **Bespoke as-is** | Doesn't recur in other intents |
| `PARTIAL_FULFILMENT_ACCEPTED`, `CANCELLATION_ACCEPTED` | B5, B8, B10 | **Alias to single canonical name** | Same audit signal across resolution-driven intents |
| **`PRICE_MISMATCH` overlap (B4 ↔ B1)** | EDI_MISMATCH with `mismatch_sub_type=PRICE_MISMATCH` | **Hard alias**: validator MUST require the operator to pick from B1's vocabulary, not B4's | Otherwise operators double-tag the same fact (EDI mapping vs contract-stale) and the audit signal collapses |

**Engineering surface for the C1 PRICE_MISMATCH alias:**
- `constraints/specs.py::INTENT_REASON_TAGS["EDI_MISMATCH"]` returns the B4 list **unless** `metadata.mismatch_sub_type == "PRICE_MISMATCH"`, in which case it returns `INTENT_REASON_TAGS["CONTRACTUAL_CORRECTION"]` (B1).
- Validator reject path: when the override `reason_tag` is in B4 and `mismatch_sub_type == "PRICE_MISMATCH"`, surface the error message "Use a CONTRACTUAL_CORRECTION reason for PRICE_MISMATCH overrides; see ADR-034 §5".
- Add an ADR-034 amendment recording the alias.

### C2. Phase 5.2 engineering land sequencing

Adopt batched land per the source doc §C2 recommendation:

| Batch | Intents | Validates |
|---|---|---|
| **5.2.a** | B1 (CONTRACTUAL_CORRECTION), B2 (CREDIT_BLOCK), B3 (MANUAL_ORDER_INTAKE) | Highest volume; exercises grandfather-validator path; exercises cosign-no-split decision (A6 / B2.5) |
| **5.2.b** | B4 (EDI_MISMATCH with B1 alias), B5 (BACK_ORDER), B6 (PRICE_HOLD_RELEASE) | Exercises sub-type-aware validator (C1 PRICE_MISMATCH); exercises bulk-action audit pattern (B5/B6) |
| **5.2.c** | B7, B8, B9, B10, B11 | Lower-volume intents; final ratification |

Each batch ships only after the batch's SMEs sign live; the consolidated proposals here are the **starting point** for the live workshops.

### C3. ML calibration handoff

| Item | Decision |
|---|---|
| Cadence (default) | **Weekly** clustering report on `reason_code_clusters` per intent. Sufficient for retraining cadence (Lin Ma confirmed). |
| Real-time stream exception | **`FALSE_POSITIVE`** in B11 streams real-time into the MASS_PRICING_ERROR detector retraining pipeline (Lin Ma's hard requirement). |
| Tag-level calibration relevance | Tabulated per intent under each B-block's `learning_signal` column (`gold` / `categorical` / `noise` / `escape hatch`). |
| Knowledge-pack handoff | `knowledge/compaction/override.template.md::reason_code_clusters` is the formal feed; engineering ships data, not dashboards. |

---

## Output deliverables

Per the source doc §"Output of each session":

1. ✅ **This file** (consolidated equivalent of 11 per-intent files; live SMEs may choose to break it apart for their own ratification artefacts).
2. ✅ **Curated 4–6-entry lists** ready for `constraints/specs.py::INTENT_REASON_TAGS[<INTENT>]` — see each B-block.
3. ⏸ **Compliance sign-off blocks** — marked "pending live ratification" throughout; treat as proposed sign-off for the live SME panel to convert to a final ☑.

## Next-action checklist (engineering)

- [ ] Live ratification workshops convened with the eight named SMEs (use this doc as the strawman pre-read; ~30 min per intent should suffice given the strawman).
- [ ] §5.2.a batch landing: B1 / B2 / B3 lists into `constraints/specs.py::INTENT_REASON_TAGS` (gated on Sandra / Maria / Priya live sign-off).
- [ ] §5.2.b batch landing: B4 / B5 / B6, including the C1 PRICE_MISMATCH sub-type-aware validator and the ADR-034 amendment.
- [ ] §5.2.c batch landing: B7 / B8 / B9 / B10 / B11.
- [ ] Audit-bearing-field grandfather entries added per CLAUDE.md Guardrail #6 for any field this doc names that is not yet on `resolution_data` / `enrichment_context` (per-tag `required_fields:` columns above).
- [ ] `knowledge/compaction/override.template.md::reason_code_clusters` updated with the per-tag `learning_signal` annotations.
- [ ] B11 `FALSE_POSITIVE` real-time stream wired to the MASS_PRICING_ERROR detector retrain pipeline (Lin Ma to specify endpoint).

---

*Authored 2026-05-10 in response to `docs/workshops/per-intent-reason-tag-questions.md` (commit `a9d4718`). The simulated panel composition stands in for the eleven 90-minute SME sessions prescribed in §28.3; live ratification is a hard gate before §5.2 engineering land.*
