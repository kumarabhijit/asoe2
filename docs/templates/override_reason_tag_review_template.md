# Per-Intent Override-Reason-Tag Review Template

**Audience:** Domain SME + Compliance reviewer.
**Purpose:** Standardised template for the Phase 5.1 per-intent reason-tag curation. One pass per intent; the output of a session is a 4–6-entry curated list that lands in `constraints/specs.py::INTENT_REASON_TAGS` (Phase 5.2).
**Time budget:** ~90 minutes per intent (45 for sample review; 30 for category proposal; 15 for Compliance sign-off).
**Pre-read:** `docs/adr/ADR-033-override-reason-code-vocabulary.md` (the existing global vocabulary), `tasks.md` Phase 5.1 / 5.2 / 5.3.

---

## How to use this template

1. Copy the body below to a new file under `docs/workshops/reason-tags/<intent-name>-<date>.md`.
2. Fill in the Intent and the data-pull command.
3. Hold the session; capture observations + proposed categories inline.
4. Land the resulting 4–6-entry list in `constraints/specs.py::INTENT_REASON_TAGS[<INTENT>]`.
5. Mark the §5.1 item ticked for that intent in `tasks.md`.

The §5.3 grandfather invariant applies: existing audit-log rows that carry tags from the previous global vocabulary must NOT be re-labelled (the hash chain breaks). The new vocabulary is forward-only; the validator change in §5.2 must allow grandfathered tags to remain valid for read.

---

## ✂ Template body — copy from here ✂

# Reason-Tag Curation — `<INTENT>`

**Date:** `YYYY-MM-DD`
**Reviewers:** Domain SME (`@name`) + Compliance (`@name`).
**Intent:** `<INTENT>` (e.g. `DUPLICATE_PO`, `CONTRACTUAL_CORRECTION`, `MANUAL_ORDER_INTAKE`).
**Status:** Draft → Compliance review → Accepted.

---

## 1. Data pull

Sample of historical override `change_reason` notes for this intent. Run:

```sql
SELECT
  e.id                                            AS exception_id,
  e.intent,
  e.created_at,
  rd ->> 'reason_tag'                             AS legacy_reason_tag,
  rd ->> 'change_reason'                          AS free_form_change_reason,
  rd ->> 'resolved_action'                        AS resolved_action,
  rd ->> 'financial_impact_usd'                   AS impact_usd
FROM exceptions e
CROSS JOIN LATERAL jsonb_extract_path(e.resolution_data, 'override') rd
WHERE e.intent = '<INTENT>'
  AND e.lifecycle_state IN ('RESOLVED','PENDING_COSIGN')
  AND e.created_at >= NOW() - INTERVAL '180 days'
ORDER BY e.created_at DESC
LIMIT 100;
```

(Adjust 180-day window per data volume. Target ~100 rows per intent so the spread of reasons is observable without becoming a multi-day reading task.)

---

## 2. Sample observations (free-form during reading)

For each of the 100 rows, jot a short tag in your notes — what does this row's reason describe? Examples:

* "customer asked for one-time discount" → `customer_concession`
* "buyer's PO had a 2-week-stale price" → `contract_stale`
* "promo window ended yesterday but PO predates it" → `promo_window`
* "duplicate detected was actually a release schedule" → `release_schedule_misclassified`
* "back-office data error in SAP master" → `data_error`
* "[doesn't fit any pattern above]" → `other`

After ~30 rows you'll see clusters emerging. After 100 rows you should have 4–8 candidate buckets. Aim to land 4–6.

### 2.1 Frequency table (filled during reading)

| Candidate bucket | Count | % of sample | Example exception_id |
|---|---|---|---|
| `<bucket-A>` | | | |
| `<bucket-B>` | | | |
| `<bucket-C>` | | | |
| `<bucket-D>` | | | |
| `other` | | | |
| **Total** | 100 | 100% | |

### 2.2 Notable patterns

Free-form notes — anything that surprised you, edge cases, sub-clusters that might warrant their own bucket if they grow:

* `…`

---

## 3. Proposed categories

Translate the candidate buckets into the final 4–6 list. Constraints:

* Names must be `snake_case`, ≤30 chars.
* `other` MUST be in every intent's set (prevents workflow dead-ends per ADR-033 §3).
* Every bucket should cover ≥5% of the sample (smaller clusters should fold into `other` or a peer).

**Final list for `<INTENT>`:**

```python
INTENT_REASON_TAGS["<INTENT>"] = (
    "<bucket_a>",
    "<bucket_b>",
    "<bucket_c>",
    "<bucket_d>",
    "other",
)
```

### 3.1 Per-bucket definition (one-sentence each)

* **`<bucket_a>`** — when …
* **`<bucket_b>`** — when …
* **`<bucket_c>`** — when …
* **`<bucket_d>`** — when …
* **`other`** — every reason that doesn't fit the above. Reviewer adds free-form `change_reason` to the override notes.

### 3.2 Anti-categories (deliberately NOT in the list)

For each candidate that did NOT make the cut, note why:

* `<rejected-candidate>` — too narrow (covered <5% of sample); folded into `<bucket_x>`.
* `<rejected-candidate>` — too broad (overlaps `<bucket_y>`); merged.
* `<rejected-candidate>` — security-sensitive; folded into `other` so the audit log doesn't surface it as a queryable category.

---

## 4. Compliance sign-off

| Compliance ask | Reviewer response |
|---|---|
| Does the proposed list cover the SOX-relevant audit slices? | |
| Are any historical reasons silently dropped (anti-grandfather)? | |
| Does `other` remain in the set? | |
| Is the validator change in §5.2 strictly additive (allows the new list AND grandfathered legacy tags for read)? | |

**Sign-off:** `@compliance-reviewer-name` `YYYY-MM-DD`

---

## 5. Implementation handoff

* `constraints/specs.py::INTENT_REASON_TAGS["<INTENT>"]` updated.
* `openapi/asoe2.openapi.json` regenerated.
* `tests/test_specs.py::TestPerIntentReasonTag::test_<intent>_categories` exercises the new vocabulary.
* `tasks.md` §5.1 entry for `<INTENT>` ticked.

---

## 6. Future-curation notes

What changed for `<INTENT>` since the last review (or "first review" for the initial pass):

* …

What we'll watch for in the next 90 days that might warrant adding a 6th category:

* …

✂ Template body — copy until here ✂

---

## Sequencing the per-intent sessions

Recommended order (highest-volume intents first so the framework is exercised on real data):

1. `DUPLICATE_PO`
2. `CONTRACTUAL_CORRECTION`
3. `CREDIT_BLOCK`
4. `MANUAL_ORDER_INTAKE`
5. `EDI_MISMATCH`
6. `BACK_ORDER`
7. `PRICE_HOLD_RELEASE`
8. `OVER_MAX`
9. `MIN_ORDER_QTY`
10. `PALLET_CONFIG`
11. `DELIVERY_DELAY`
12. `MASS_PRICING_ERROR`

Each session produces one curated list. The Phase 5.2 wiring + Phase 5.3 grandfather-validator change can land **after the first 3 intents** are reviewed, so the mechanism is proven before the rest of the curation completes. The remaining intents land their lists incrementally.

---

*Template authored 2026-05-09 alongside the virtual-workshop pre-read. Review cadence: revisit this template after the first 3 sessions to fold lessons learned into v2.*
