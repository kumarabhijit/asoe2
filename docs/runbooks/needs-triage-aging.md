# Runbook — NEEDS_TRIAGE Aging

> **Authority:** `docs/specs/case-intent-supergroup-requirements.md` §8.2 (forcing functions).
> **Owner:** Team Lead (per-shift); Steward (weekly review).

`SG_NEEDS_TRIAGE` is the reserved super-group for cases the classifier cannot place on intake. The system enforces four forcing functions so the bucket can't quietly absorb 30–60 % of volume the way unmoderated "OTHER" buckets historically do:

| # | Forcing function | Where it lives | Status |
|---|---|---|---|
| 1 | **Hard-block at close** | `CaseStore.update` raises `NeedsTriageCloseBlocked` on RESOLVED transition | ✅ shipped (commit a3fde91) |
| 2 | **48 h auto-age alert** | Team-lead dashboard / scheduled scan | ⏳ ops integration pending |
| 3 | **Weekly Top-10 reasons** | Steward report from `case_classification_history.reason_text` | ⏳ steward tool pending |
| 4 | **Per-CSR scorecard `< 3 %`** | CS-Ops dashboard | ⏳ analytics integration pending |

This runbook describes the operational workflow built on (1) and what to do until (2)–(4) wire up.

---

## #1 — Hard-block at close (active)

A case with `supergroup_code == 'SG_NEEDS_TRIAGE'` cannot transition to `RESOLVED`. The store raises `NeedsTriageCloseBlocked` (subclass of `Exception`, not `ValueError` — explicit failure). The API surfaces this as a 409 Conflict.

**Operator response:** reclassify the case to a real super-group *before* attempting to resolve. The escape hatch is the same `case_store.update` call that put the case into triage, just with the right super-group:

```python
case_store.update(
    case_id,
    supergroup_code="SG_NEW_ORDER",  # real classification
    classified_by="user:lead-1",
    classifier_type="HUMAN",
    reason_text="Triage complete — order intake",
)
case_store.update(case_id, status="RESOLVED")
```

Or atomically in one call:

```python
case_store.update(
    case_id,
    supergroup_code="SG_NEW_ORDER",
    status="RESOLVED",
    classified_by="user:lead-1",
    classifier_type="HUMAN",
)
```

Every reclassification appends one row to `case_classification_history` (criterion #9).

---

## #2 — 48 h auto-age alert (pending wiring)

Target query (drop-in for a scheduled scan):

```sql
SELECT oc.case_id, oc.tenant_id, oc.opened_at,
       (julianday('now') - julianday(oc.opened_at)) * 24 AS hours_in_triage
FROM order_case oc
WHERE oc.supergroup_code = 'SG_NEEDS_TRIAGE'
  AND oc.status NOT IN ('RESOLVED','FAILED','BLOCKED')
  AND (julianday('now') - julianday(oc.opened_at)) * 24 > 48
ORDER BY hours_in_triage DESC;
```

When wired, this should:

1. Run hourly via the existing scheduler infra (same pattern as `scripts/run_drift_forwarder.py`).
2. Post each over-aged case to the team-lead Slack channel `#ops-needs-triage` with a deep link to `/cases/{case_id}`.
3. Tag the team lead on the case if no human classified it within the last 6 hours (read the latest `case_classification_history` row for that case).

The case-store API already supports the read: `case_store.list_by_tenant(tenant_id)` filtered on `supergroup_code == 'SG_NEEDS_TRIAGE'` + age math on `opened_at`. The Slack post + lead-tagging are integration concerns owned by the ops team.

---

## #3 — Weekly Top-10 reasons (pending steward tool)

Target query:

```sql
SELECT reason_text, COUNT(*) AS occurrences
FROM case_classification_history
WHERE supergroup_code = 'SG_NEEDS_TRIAGE'
  AND reason_text IS NOT NULL
  AND classified_at >= datetime('now', '-7 days')
GROUP BY reason_text
ORDER BY occurrences DESC
LIMIT 10;
```

The steward reviews the Top-10 weekly. Any reason that appears 3+ times in two consecutive weeks is a candidate for a new super-group or leaf intent — file a `steward_change add-intent` proposal (see `docs/runbooks/taxonomy-change.md`).

The query above runs cleanly against the V020 schema today. Wiring it into a recurring report is the next concrete deliverable; for now the Steward runs it ad-hoc.

---

## #4 — Per-CSR scorecard `< 3 %` (pending analytics)

Target metric (per CSR per quarter):

```
needs_triage_rate = (
    cases_where_first_classification_was_SG_NEEDS_TRIAGE_by_this_csr
    / total_cases_classified_by_this_csr
)
```

The numerator reads `case_classification_history` filtered on `classifier_type='HUMAN'`, `supergroup_code='SG_NEEDS_TRIAGE'`, grouped by `classified_by`, AND the first row per `case_id` (so a later re-triage doesn't double-count).

Target: < 3 % per CSR. CSRs above 5 % trigger a 1:1 with the team lead — the floor is "real ambiguity" not "didn't try to classify".

---

## Escalation path

| Condition | Owner | Action |
|---|---|---|
| Aging > 48 h | Team Lead | Reclassify or escalate. |
| Aging > 72 h | OM Business Lead | Page lead; case must close or be re-routed. |
| Top-10 reason repeats 2+ weeks | Steward | File `add-intent` proposal. |
| CSR > 5 % triage rate (quarter) | Team Lead | 1:1 + training. |
| CSR > 10 % triage rate (quarter) | OM Business Lead | Performance review. |

---

## What this runbook deliberately does NOT cover

- **Re-routing a NEEDS_TRIAGE case to a different tenant or partner**: out of scope; tenants are isolated per ADR-028.
- **Bulk reclassification of historical NEEDS_TRIAGE cases**: forward-only; each reclassification must be attributable, so a one-shot SQL update is not appropriate. Use the steward CLI or a per-case loop with HUMAN attribution.
