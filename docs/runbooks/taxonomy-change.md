# Runbook — Case Taxonomy Change

> **Authority:** `docs/specs/case-intent-supergroup-requirements.md` §9
> (steward change-control), §3.9 (SAP block-code reconciliation).
> **Owner:** Master Data Steward.
> **SLA:** routine change — 3 business days; emergency hot-fix — same day with retroactive CAB ≤ 48 h.

This runbook covers four steward operations on the case taxonomy:

1. **Add a new leaf intent** — e.g. a new SAP block code surfaced by nightly reconciliation.
2. **Add a new super-group** — rare; needs PO + OM-lead sign-off (§9.1).
3. **Deprecate an intent or super-group** — replace, retire, or split.
4. **Hot-fix path** — for a P2 alert from the reconciliation cron.

The source of truth is **`db/seeds/case_taxonomy.yaml`** (committed). The CLI at `scripts/steward_change.py` is the supported way to edit it — direct hand-edits are allowed but skip the validation guardrails.

---

## Routine change — add a new leaf intent

Trigger: a new SAP block code appears in the nightly reconciliation report (see `docs/runbooks/sap-block-reconciliation.md`) or a CS-Ops ticket requests a new CUSTOMER super-group leaf.

1. **Open a fresh branch.**
   ```sh
   git checkout -b taxonomy/add-pallet-damage
   ```

2. **Run the steward CLI.**
   ```sh
   python -m scripts.steward_change add-intent \
       --code INT_PALLET_DAMAGE \
       --supergroup SG_BLOCK_LOGISTICS \
       --description "Receipt rejected because pallet is damaged on arrival" \
       --sap-block-code ZD \
       --sap-block-field LIFSP \
       --display-name "Pallet Damaged"
   ```
   The CLI writes the row into the YAML, regenerates the Python +
   TypeScript constants, and re-runs schema + invariant validation. If any
   step fails the CLI exits non-zero with a precise error and leaves the
   YAML in the proposed state for inspection.

3. **Review the diff.**
   ```sh
   git diff db/seeds/case_taxonomy.yaml \
            contracts/_generated/taxonomy_constants.py \
            asoe-ui/src/generated/taxonomy.ts
   ```
   Verify:
   - the YAML row landed in the right section (intents block),
   - the generated constants include the new code,
   - no unrelated rows shifted (the CLI preserves insertion order).

4. **Commit and open PR.**
   ```sh
   git add db/seeds/case_taxonomy.yaml \
           contracts/_generated/taxonomy_constants.py \
           asoe-ui/src/generated/taxonomy.ts
   git commit -m "taxonomy: add INT_PALLET_DAMAGE under SG_BLOCK_LOGISTICS"
   git push -u origin taxonomy/add-pallet-damage
   gh pr create --draft \
       --title "taxonomy: add INT_PALLET_DAMAGE" \
       --body "..."
   ```

5. **Get approvals.** Per §9.1:
   - **Steward** (the author of the PR).
   - **OM Business Lead** (approves the business meaning).
   - **SAP Functional Lead** (co-signs if SAP semantics change — i.e. for any new `sap_block_code` mapping).
   - **Engineering** reviews migration safety (CI runs the drift + invariant tests).

6. **Merge.** Reconciliation should now report this code as `matched` instead of `new in SAP`.

**Calendar target: ≤ 3 business days end-to-end.** If approvals slip, escalate to the OM Business Lead — do not unblock by lowering the bar.

---

## Add a new super-group

Super-groups are coarser than leaves and rarely added. Each change is a noticeable shift in the reporting taxonomy — PO and OM-Lead approvals are mandatory.

```sh
python -m scripts.steward_change add-supergroup \
    --code SG_BLOCK_TAX \
    --origin API \
    --description "Tax / VAT-ID missing on EU cross-border ship-to" \
    --owner-role tax_ops \
    --display-name "Tax / VAT Block"
```

After the CLI completes, follow steps 3–6 above. The PR title should be `taxonomy: add SG_<NAME>` so search picks it up.

---

## Deprecate an intent

Use this when:
- the underlying SAP block code is retired,
- a leaf is being replaced by a more specific one,
- a leaf has been dormant for 4+ quarters (per §9.1 governance review).

```sh
python -m scripts.steward_change deprecate-intent \
    --code INT_DELIVERY_DELAY \
    --replaced-by INT_ATP_SHORTAGE \
    --effective 2026-07-01
```

`--effective` defaults to today; pass a future date to stage the deprecation. After the effective date, the leaf-validity DB trigger (V018) rejects any new child case with this intent. Existing cases keep their classification rows in `case_classification_history` — those rows reference the historical taxonomy version (V020 `taxonomy_version` column) so audit reads remain coherent.

The CLI refuses to deprecate an already-deprecated intent and refuses to point `--replaced-by` at a non-existent code.

---

## Hot-fix path (P2 alert from reconciliation cron)

Trigger: the nightly reconciliation job (see `docs/runbooks/sap-block-reconciliation.md`) opens a GitHub issue with subject `[steward] new SAP block code <CODE>` and the run exit code was 2.

1. **Map the code immediately** (same day) — run `add-intent` with the right `--sap-block-code` / `--sap-block-field`. If you don't know the field, `LIFSK` is the safest default for delivery blocks; mark `--phase-zero-pending` so a follow-up review is tracked.

2. **Open the PR with the `hotfix-taxonomy` label**. CI runs as usual but reviewers know to prioritize.

3. **Merge before the next reconciliation run (24 h window)** so the alert clears automatically.

4. **Retroactive CAB within 48 h**: file a brief in the steward queue documenting:
   - the SAP code,
   - the leaf it was mapped to,
   - the business justification.
   The CAB review confirms the mapping was correct; if not, a follow-up `deprecate-intent` + `add-intent` replaces it.

---

## Validation

Before opening any PR, run:

```sh
python -m scripts.steward_change validate
```

Exit codes:
- `0` — YAML parses, invariants hold, generated constants are byte-stable.
- `4` — YAML is valid but the committed generated constants are out of sync (run the generator).
- `5` — YAML is invalid (schema or invariant violation; the CLI emits the precise error).

CI runs the same checks via `tests/test_taxonomy_constants_drift.py` and `tests/test_taxonomy_invariants.py`. Failing those locally first saves a review round-trip.

---

## What this runbook deliberately does NOT cover

- **UI label translations.** The `intent_label` table accepts per-locale rows; regional OM leads own those edits via a separate workflow (`docs/runbooks/taxonomy-localization.md`, follow-up).
- **Per-tenant Z-code overrides.** The `case_intent.sap_sales_org` column is currently unused; multi-tenant taxonomy variants are out of scope per requirements §2.
- **Rolling back a merged taxonomy change.** Forward-only by policy — revert via `deprecate-intent` + (optionally) `add-intent` for the replacement. Never delete a row from the YAML once merged; the audit trail in `case_classification_history` references it.
