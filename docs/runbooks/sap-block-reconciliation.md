# Runbook — SAP Block-Code Reconciliation

> **Authority:** `docs/specs/case-intent-supergroup-requirements.md` §3.9, §8.8.
> **Schedule:** nightly cron at 02:00 UTC (`.github/workflows/sap-block-reconciliation.yml`).
> **Owner:** Master Data Steward.
> **SLA:** unmapped block code surfaces → mapping committed within 1 business day.

This runbook covers the nightly drift detection between SAP's block-code codebook and the case-taxonomy seed at `db/seeds/case_taxonomy.yaml`.

---

## What the job does

`scripts/reconcile_sap_block_codes.py` reads:

- A CSV snapshot of SAP's active block codes — columns `sap_block_code,sap_block_field,description` keyed on the (code, field) pair because the same 2-char code is reused across SAP tables (`LIFSK '01'` ≠ `FAKSK '01'`).
- The active rows of `case_intent` where `sap_block_code IS NOT NULL`.

It produces a four-bucket report:

| Bucket | Meaning | Steward action |
|---|---|---|
| **matched** | (code, field) present on both sides | No action. |
| **new in SAP** | Code in snapshot, no `case_intent` row | Map within 1 business day (`steward_change add-intent`). Cases hitting this code in the meantime open with `SG_BLOCK_UNMAPPED` per §8.8. |
| **stale in DB** | `case_intent` has the code, snapshot doesn't | Code retired in SAP. Run `steward_change deprecate-intent`. |
| **malformed in DB** | `sap_block_code` set but `sap_block_field` is NULL or outside the allowed enum | Seed bug. Fix the YAML row directly. |

`has_drift` is `True` when any of new/stale/malformed is non-empty.

---

## How the cron is wired

The workflow `.github/workflows/sap-block-reconciliation.yml` runs at 02:00 UTC daily:

1. Checks out main.
2. Applies migrations into a fresh SQLite DB (the seed YAML is the input).
3. Pulls the SAP snapshot from `s3://asoe-sap-snapshots/sap_block_codes.csv` *(placeholder path — wire the real S3/HTTPS source per deployment)*.
4. Runs `python -m scripts.reconcile_sap_block_codes --sap-snapshot ... --database-url sqlite:///... --exit-nonzero-on-drift`.
5. On exit code `2` (drift detected) opens a GitHub issue with title `[steward] SAP block-code drift detected` and the full report in the body.
6. On exit code `0` (clean) writes nothing.
7. On exit code `1` (script error) opens a P1 issue tagged `ops:reconciliation-broken`.

The drift issue is the cue for a steward to follow `docs/runbooks/taxonomy-change.md`'s hot-fix path.

---

## Manual run (local / ad-hoc)

```sh
# Apply migrations into a scratch DB.
rm -f /tmp/recon.db
DATABASE_URL=sqlite:////tmp/recon.db python -m db.migrations.runner

# Run reconciliation against a local snapshot.
python -m scripts.reconcile_sap_block_codes \
    --sap-snapshot path/to/sap_block_codes.csv \
    --database-url sqlite:////tmp/recon.db \
    --exit-nonzero-on-drift
```

Exit `2` with drift; `0` otherwise.

---

## Common scenarios

### "I see 3 codes in 'new in SAP' but Phase 0 already covers them."

Phase 0's seed has placeholders with `sap_block_code: null` and `phase_zero_pending: true`. They were never mapped to real SAP codes. Run:

```sh
python -m scripts.steward_change add-intent \
    --code INT_NEW_LEAF --supergroup SG_BLOCK_X \
    --sap-block-code ZP --sap-block-field LIFSK \
    --description "..."
```

The new row replaces the placeholder on the next reconciliation run (or use `deprecate-intent` to retire the placeholder if it's no longer needed).

### "Reconciliation has been red for 3 nights — same code each time."

The steward SLA is 1 business day. After 24 h, the steward is paged. After 72 h, the alert escalates to the OM business lead per §9.1.

If the code is genuinely ambiguous (e.g. a custom Z-code with no business owner), open it under `SG_BLOCK_UNMAPPED` with `INT_UNMAPPED_PENDING_TAXONOMY` and `phase_zero_pending: true`. That lets cases proceed while the mapping is researched. Reconciliation will keep flagging it; that's the forcing function.

### "Snapshot loader rejected a row — `LIFSL` not in the allowed fields."

A typo in the snapshot upstream. The script refuses to process it (would silently produce a meaningless drift report). Fix the snapshot source; do not edit `ALLOWED_SAP_FIELDS` in the script.

### "Malformed-in-DB row surfaced."

A `case_intent` row has `sap_block_code` set but `sap_block_field` NULL. The V017 schema check should prevent this — if it leaked through it's a migration bug or manual SQL bypass. Fix the YAML row to include `sap_block_field`, regenerate constants, commit.

---

## What this runbook deliberately does NOT cover

- **The S3 snapshot upstream**: how SAP exports `TVLST`/`TVFST`/`TVAGT` joined into the CSV. That's a SAP-side ETL owned by the data team; this script consumes the artefact.
- **Steward CLI mechanics**: see `docs/runbooks/taxonomy-change.md`.
- **NEEDS_TRIAGE aging**: a separate alert path (`docs/runbooks/needs-triage-aging.md`, follow-up).
