# Case & Intent Super-Group — Implementation Plan

> **Authority:** `docs/specs/case-intent-supergroup-requirements.md` (PO-signed).
> **Principle:** minimal-and-complete changes. Touch what the requirement demands; remove smell exposed by the change; keep everything else still.
> **Do not start any phase until its predecessor's exit gate is green.**

---

## 0. Reading order & ground rules

- Every file path is absolute or relative to repo root (`/asoe2` or `/asoe-ui`).
- Migration numbering: latest existing is `V016`. New migrations are `V017+`.
- DB **table** for child cases stays named `exceptions` (one row added by V009 already carries `parent_case_id`). Python class and API/UI surface are renamed to `ChildCase` per the requirements §3 glossary; a SQL view `child_case` is added for BI consumers.
- Tests follow existing conventions: pytest `tests/test_*.py` (backend), vitest `tests/**/*.test.ts` (UI), Playwright `e2e/*.spec.ts` (UI e2e).
- Each phase ends with a **green-bar test**: a single command whose pass means the exit gate is met.

---

## 1. Architectural decisions made *inside* this plan

These are derived from the requirements, not new choices, but called out because they shape multiple phases.

| # | Decision | Why |
|---|---|---|
| D1 | Taxonomy source of truth = versioned YAML at `db/seeds/case_taxonomy.yaml`. Migration V017 seeds DB from YAML. CI regenerates Python and TS constants and fails if drift. | Steward writes YAML, no engineering involvement for routine adds (§9.1). One source. |
| D2 | Python class `ExceptionRecord` → renamed to `ChildCase` everywhere. DB table `exceptions` unchanged. New SQL view `child_case` over `exceptions`. | Naming honesty (§3 glossary). Migration safety. |
| D3 | Remove the static `Intent` / `CaseType` / `EmailClassification` enums in `contracts/models.py`. Replace with generated `_generated/taxonomy_constants.py`. | Single source of truth (D1). Removes the "second enum" smell exposed by the requirement. |
| D4 | `OrderCase.tier` (`CaseTier = Literal[1, 2, 3]`) extends to 4 to match §8.4's tier bucketing. | Requirement-driven. Backfill maps existing values cleanly: old 1→1, 2→2, 3→3, new 4 only on new cases. |
| D5 | `divergence_reason text NULL` ships in Phase 2 schema but is unused in v1 (requirements §8.1). Inheritance trigger reads `app_config.inheritance_mode_customer` (default `STRICT`). | Latent capability per PO-approved scope; no schema change later. |
| D6 | Deprecated columns (`case_type`, `email_classification`, `source`) are kept as **generated columns** for one release; dropped in a separate post-Phase-5 migration. | Requirement §10 explicit. Keeps downstream BI alive across the cutover. |
| D7 | No new routing logic in this work. Routing already dispatches on the existing `intent` field; we rename the column to `intent_code` and point it at the new lookup table — dispatch code paths are otherwise untouched. | Requirements §8.5: "Routing on leaf, super-group is never a routing input". The current code already obeys this; we don't refactor what isn't broken. |

---

## 2. Phase 0 — Data-mining sprint (no code in this repo)

**Out of scope for this plan.** Produces two artefacts the implementation consumes:

1. `db/seeds/case_taxonomy.yaml` (initial version) — committed at start of Phase 1.
2. A short memo (`docs/specs/case-intent-supergroup-seed-evidence.md`) summarising the SAP block-code extract and 30-day email audit. PO reviews and signs off on the seed.

**Exit gate:** PO signature on the seed memo. Phase 1 starts.

---

## 3. Phase 1 — Lookup tables + constant generation (1 wk)

### 3.1 Files added

| Path | Purpose |
|---|---|
| `db/migrations/V017__case_taxonomy.sql` | Creates `case_supergroup`, `case_intent`, `supergroup_intent_allowed`, `intent_label`. Seeds from a SQL `COPY` of pre-generated CSVs (or `INSERT` literals) that the loader script emitted. |
| `db/seeds/case_taxonomy.yaml` | Steward-owned source of truth. Schema documented inline. |
| `db/seeds/case_taxonomy.schema.json` | JSON-schema validation of the YAML. CI rejects malformed seed. |
| `scripts/seed_taxonomy.py` | Loads YAML → DB (idempotent UPSERT). Used by V017 and by Steward workflows. |
| `scripts/generate_taxonomy_constants.py` | Reads YAML → emits two files: `contracts/_generated/taxonomy_constants.py`, `asoe-ui/src/generated/taxonomy.ts`. Pure function of the YAML. |
| `contracts/_generated/taxonomy_constants.py` | Generated, **committed**. Re-running the generator must produce a byte-identical file. |
| `contracts/_generated/__init__.py` | Empty package marker. |
| `asoe-ui/src/generated/taxonomy.ts` | Generated, committed. |
| `tests/test_taxonomy_seed.py` | (a) YAML validates against JSON-schema. (b) `seed_taxonomy.py` is idempotent. (c) DB rows match YAML row-for-row. |
| `tests/test_taxonomy_constants_drift.py` | Re-runs the generator in a temp dir and diffs against the committed file. Fails on drift. **This is the CI guard.** |
| `tests/test_taxonomy_invariants.py` | Every intent's `supergroup_code` exists in `case_supergroup`. Every `(sg, intent)` pair in `supergroup_intent_allowed` has both ends active. No two active rows share an SAP block code. |
| `asoe-ui/tests/contract/test_taxonomy_generated_shape.test.ts` | Generated TS file exports the expected named unions and the supergroup→intent map. |

### 3.2 Files modified

| Path | Change |
|---|---|
| `.github/workflows/tests.yml` (or equivalent) | Add a step `python scripts/generate_taxonomy_constants.py --check` before pytest. Fails the build on drift. |
| `pyproject.toml` | Add `[tool.taxonomy] yaml_path = "db/seeds/case_taxonomy.yaml"`. Add `scripts.generate-taxonomy = "scripts.generate_taxonomy_constants:main"`. |
| `Makefile` (or add one) | Targets `taxonomy-gen`, `taxonomy-check`, `taxonomy-seed`. |

### 3.3 Files removed

None this phase. The `Intent` enum stays alive until Phase 2 finishes its rename.

### 3.4 Phase 1 exit gate

```
pytest tests/test_taxonomy_seed.py tests/test_taxonomy_constants_drift.py tests/test_taxonomy_invariants.py
asoe-ui$ npx vitest tests/contract/test_taxonomy_generated_shape.test.ts
```

All green. No production code yet consumes the new tables — Phase 1 is contracts plumbing only.

---

## 4. Phase 2 — Case model migration (2 wk)

### 4.1 Files added

| Path | Purpose |
|---|---|
| `db/migrations/V018__case_origin_supergroup.sql` | (a) Adds columns to `order_case` and `exceptions`. (b) Creates `app_config` table (one row: `inheritance_mode_customer = 'STRICT'`). (c) Creates `child_case` view over `exceptions`. (d) Inheritance trigger + leaf-validity trigger. (e) Backfill block (see §4.4). (f) Generated columns for deprecation (D6). |
| `db/migrations/V018__case_origin_supergroup.down.sql` | Reverse for local-dev rollback. |
| `tests/test_case_origin_inheritance.py` | API parent rejects divergent child supergroup. CUSTOMER parent under STRICT also rejects. Flipping config to RELAXED + `divergence_reason` provided → accepts. Required for criteria #2, #3, #8.1. |
| `tests/test_case_leaf_validity.py` | `(supergroup_code, intent_code)` not in `supergroup_intent_allowed` for effective date → insert rejected. Criterion #4. |
| `tests/test_case_unmapped_block_code.py` | SAP block code with no `case_intent` row → case created with `SG_BLOCK_UNMAPPED` and a row in the ops-alerts queue. Criterion #8. |
| `tests/test_case_backfill_supergroup.py` | Pre-V018 fixture cases with `source='manual_order'` + `email_classification='NEW_ORDER'` backfill to `origin='CUSTOMER'`, `supergroup_code='SG_NEW_ORDER'`. Likewise `automated_order` + `sap_block_code` → `API` + correct `SG_BLOCK_*`. Edge: orphan `email_classification='OTHER'` → `SG_NEEDS_TRIAGE`. Edge: unrecognised `sap_block_code` → `SG_BLOCK_UNMAPPED`. |
| `tests/test_child_case_view.py` | View returns expected column shape; column renames in view do not regress consumers. |

### 4.2 Files modified

| Path | Change |
|---|---|
| `contracts/models.py` | **Remove** lines 11–24 (Intent enum), 151 (CaseSource), 162 (CaseType), 178–184 (EmailClassification). **Remove** validators `_default_case_type_from_source`, `_check_case_type_invariants` (now meaningless). **Add** `Origin = Literal["CUSTOMER", "API"]`. **Extend** `CaseTier` from `Literal[1,2,3]` to `Literal[1,2,3,4]`. **Add** new `OrderCase` fields: `origin: Origin`, `supergroup_code: str`, `predecessor_case_id: UUID | None`, `will_miss_rdd: bool = False`, `sla_due_at: datetime`. **Replace** `intent` import with `from contracts._generated.taxonomy_constants import IntentCode, SupergroupCode`. **Rename** class `ExceptionRecord` → `ChildCase` (along with all imports). Add fields `supergroup_code`, `divergence_reason: str | None = None`, `intent_code: IntentCode` (replaces `intent`), `sap_block_field: Literal["LIFSK","LIFSP","FAKSK","FAKSP","ABGRU","CMGST","Z_CUSTOM"] | None`, `scope: Literal["HEADER","ITEM"] | None`. |
| `api/store.py` | Rename `ExceptionRecord` (lines 26–102) → `ChildCase`. Update `to_summary()` and `to_detail()` to surface new fields and drop `case_type`, `email_classification`. Update `CaseStore.lookup_or_create()` to take `origin` and `supergroup_code` instead of `source` and `email_classification`. |
| `api/schemas.py` | Rename `ExceptionSummary` → `ChildCaseSummary` (line 252), `ExceptionDetailResponse` → `ChildCaseDetailResponse` (line 283). Add `origin`, `supergroup_code`, `predecessor_case_id` to case response models. Drop `case_type`, `email_classification` from response models (still in DB as deprecated columns, but not surfaced — per minimal-API-surface principle). |
| `api/routes/cases.py` | Update endpoint handlers to read/write new fields. JSON response keys: `child_cases` (was `exceptions`), `supergroup_code`, `origin`. |
| `api/routes/health.py` | Replace `_ALLOWED_INTENTS = list(AllowedIntent.__args__)` (line 30–40) with a query that reads active rows from `case_intent`. Add `allowed_supergroups_by_origin: dict[str, list[str]]` to the response. Cache for 60s in-process. |
| `skills/intent_classifier.py` | Generation-constrained set comes from the live `case_intent` query (or generated constants — pick generated constants to keep the LLM call self-contained). Output field renamed `intent` → `intent_code`. |
| `orchestration/nodes.py` | Wherever `case_type` or `email_classification` is read (full grep needed during implementation), switch to `supergroup_code` from the case. No logic change unless a node was branching on the old discriminator. |
| `agents/backfill.py` | Pass 1 (lines 68–116) — derive `origin` from `event.source`, derive `supergroup_code` from sap_block_code (API) or email classification (CUSTOMER). Pass 2 — no functional change beyond the rename. |
| `recipes/registry.py` | `allowed_intents` tuples — values change from raw strings to `IntentCode` literal references. The tuple shape is unchanged, so dispatch code is untouched. |
| `tests/test_routes_cases.py`, `tests/test_routes_cases_records.py`, `tests/test_run_backfill_script.py`, `tests/test_health_autonomy.py` | Update assertions to new field names and shapes. Add coverage for `origin` and `supergroup_code` in API responses. **Do not delete existing tests** unless they assert a behaviour the requirement explicitly removes (`case_type`, `email_classification`). |

### 4.3 Files removed

| Path | Reason |
|---|---|
| `asoe-ui/tests/lib/cases_api_case_type.test.ts` | Tests `case_type` inference, which is removed from the response surface. The behaviour it covers no longer exists. |

(All other deletions are at the line/symbol level, listed in §4.2.)

### 4.4 V018 migration anatomy (sketch — final SQL written during implementation)

```sql
-- 1. app_config
CREATE TABLE app_config (key text PRIMARY KEY, value text NOT NULL, updated_at timestamptz);
INSERT INTO app_config VALUES ('inheritance_mode_customer', 'STRICT', now());

-- 2. order_case columns
ALTER TABLE order_case
  ADD COLUMN origin            text REFERENCES case_supergroup(origin) /* logical */,
  ADD COLUMN supergroup_code   text REFERENCES case_supergroup(code),
  ADD COLUMN predecessor_case_id uuid REFERENCES order_case(case_id),
  ADD COLUMN will_miss_rdd    boolean NOT NULL DEFAULT false,
  ADD COLUMN sla_due_at       timestamptz;
ALTER TABLE order_case ALTER COLUMN tier TYPE smallint;  -- already; relax 1..3 → 1..4

-- 3. exceptions columns
ALTER TABLE exceptions
  ADD COLUMN supergroup_code  text REFERENCES case_supergroup(code),
  ADD COLUMN divergence_reason text,
  ADD COLUMN intent_code      text REFERENCES case_intent(code),
  ADD COLUMN sap_block_field  text CHECK (sap_block_field IN ('LIFSK','LIFSP','FAKSK','FAKSP','ABGRU','CMGST','Z_CUSTOM')),
  ADD COLUMN scope            text CHECK (scope IN ('HEADER','ITEM'));

-- 4. Backfill (single transaction with the DDL)
UPDATE order_case SET origin = CASE source
    WHEN 'manual_order' THEN 'CUSTOMER'
    WHEN 'automated_order' THEN 'API' END;
UPDATE order_case SET supergroup_code = COALESCE(
    (SELECT 'SG_' || email_classification WHERE source='manual_order' AND email_classification IS NOT NULL),
    (SELECT supergroup_code FROM case_intent WHERE case_intent.sap_block_code = (
        SELECT sap_block_code FROM exceptions WHERE parent_case_id = order_case.case_id LIMIT 1)),
    'SG_NEEDS_TRIAGE');
UPDATE exceptions SET intent_code = 'INT_' || intent;  -- rely on renamed taxonomy
UPDATE exceptions e SET supergroup_code = (SELECT supergroup_code FROM order_case oc WHERE oc.case_id = e.parent_case_id);

-- 5. Triggers
CREATE FUNCTION enforce_child_inheritance() RETURNS trigger AS $$ ... $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_child_inherit BEFORE INSERT OR UPDATE OF supergroup_code ON exceptions
   FOR EACH ROW EXECUTE FUNCTION enforce_child_inheritance();

CREATE FUNCTION enforce_leaf_validity() RETURNS trigger AS $$ ... $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_leaf_valid BEFORE INSERT OR UPDATE OF intent_code, supergroup_code ON exceptions
   FOR EACH ROW EXECUTE FUNCTION enforce_leaf_validity();

-- 6. NOT NULL + post-backfill constraints
ALTER TABLE order_case ALTER COLUMN origin SET NOT NULL;
ALTER TABLE order_case ALTER COLUMN supergroup_code SET NOT NULL;
ALTER TABLE exceptions ALTER COLUMN intent_code SET NOT NULL;
ALTER TABLE exceptions ALTER COLUMN supergroup_code SET NOT NULL;

-- 7. Deprecation generated columns (D6)
ALTER TABLE order_case
  ALTER COLUMN source SET GENERATED ALWAYS AS (CASE origin WHEN 'CUSTOMER' THEN 'manual_order' ELSE 'automated_order' END);
-- (and similar for case_type, email_classification — read-only consumers keep working)

-- 8. View
CREATE VIEW child_case AS SELECT
  id AS child_case_id, parent_case_id AS case_id, tenant_id, supergroup_code,
  intent_code, divergence_reason, sap_block_code, sap_block_field, scope,
  lifecycle_state, shadow_verdict, /* ... */
FROM exceptions;
```

### 4.5 Phase 2 exit gate

```
pytest tests/test_case_origin_inheritance.py \
       tests/test_case_leaf_validity.py \
       tests/test_case_unmapped_block_code.py \
       tests/test_case_backfill_supergroup.py \
       tests/test_child_case_view.py \
       tests/test_routes_cases.py \
       tests/test_routes_cases_records.py \
       tests/test_run_backfill_script.py \
       tests/test_health_autonomy.py
```

Acceptance criteria covered after Phase 2: **#1, #2, #3, #4, #6, #8, #11, #12.**

---

## 5. Phase 3 — Classification audit + NEEDS_TRIAGE forcing functions (1 wk)

### 5.1 Files added

| Path | Purpose |
|---|---|
| `db/migrations/V020__case_classification_history.sql` | Creates `case_classification_history` table per requirements §8.6. **V020 not V019** — V019 was assigned to the legacy-column drop migration earlier in Phase 2. Postgres triggers (`tg_cch_no_update`, `tg_cch_no_delete`, `tg_cch_no_truncate`) block all mutation paths; SQLite mirror uses equivalent `BEFORE` triggers. `REVOKE UPDATE, DELETE, TRUNCATE` from the app role is documented in the SQL comment as the defence-in-depth layer applied per environment by the deploy pipeline (not by this migration, since role names vary). |
| `tests/test_case_classification_history.py` | One classification → exactly one history row. Reclassification → second row, both readable in order. App role `app_user` cannot UPDATE/DELETE (Postgres permission test). Taxonomy version stamped correctly. Criterion #9. |
| `tests/test_needs_triage_close_block.py` | Case with `supergroup_code='SG_NEEDS_TRIAGE'` rejects transition to any RESOLVED state. After reclassification to a real supergroup, RESOLVED is accepted. Criterion #5. |
| `tests/test_needs_triage_age_alert.py` | Case in NEEDS_TRIAGE > 48h surfaces in the steward dashboard query. |
| `tests/test_routing_leaf_only.py` | Synthetic: same `intent_code`, different `supergroup_code` → same `queue_id`. Different `intent_code`, same `supergroup_code` → potentially different `queue_id`. Criterion #7. |
| `tests/test_reclassification_rights.py` | Matrix per §8.3. CSR-on-own-case-within-24h: allowed. CSR-on-others-case: rejected. Lead-on-any-open: allowed. Model-confidence<0.85: rejected; ≥0.85: allowed and writes `classifier_type='MODEL'`. |

### 5.2 Files modified

| Path | Change |
|---|---|
| `api/store.py` | `CaseStore.transition_status()` (or equivalent) gains a guard: if `target_status ∈ RESOLVED_STATES` and `case.supergroup_code == 'SG_NEEDS_TRIAGE'`, raise `NeedsTriageCloseBlocked`. |
| `api/routes/cases.py` | Add `GET /api/v1/cases/{case_id}/classification-history`. Add `GET /api/v1/steward/needs-triage-aging` (steward-role-gated). |
| `api/schemas.py` | Add `ClassificationHistoryEntry` response model. |
| `skills/intent_classifier.py` | After classification or reclassification, call `case_store.record_reclassification(...)` which writes the history row (trigger covers the DB side, but the app should also pass `classifier_type` and `model_version` — these are not derivable from DDL alone). Add the confidence-threshold gate from §8.3. |
| `orchestration/nodes.py` | Same: any node that mutates classification calls the recorder. Audit the nodes during implementation and list them in the PR description. |
| `recipes/registry.py` | No change. Verifies criterion #7 (routing on leaf) passively — adding the new test is enough. |

### 5.3 Phase 3 exit gate

```
pytest tests/test_case_classification_history.py \
       tests/test_needs_triage_close_block.py \
       tests/test_needs_triage_age_alert.py \
       tests/test_routing_leaf_only.py \
       tests/test_reclassification_rights.py
```

Acceptance criteria added: **#5, #7, #9.**

---

## 6. Phase 4 — UI label resolution + locale (1 wk)

### 6.1 Files added

| Path | Purpose |
|---|---|
| `asoe-ui/src/hooks/useIntentLabel.ts` | Hook: `useIntentLabel(code: string, domain: "SUPERGROUP" \| "INTENT"): string`. Reads the labels map fetched from the new API endpoint, with locale fallback (requested → `en` → code). |
| `asoe-ui/src/hooks/useLabels.ts` | Single-flight fetcher of the whole label map for current locale. Cached in React Query for 5 min. |
| `api/routes/labels.py` | `GET /api/v1/labels?locale=<bcp47>` → returns `{supergroups: {code: name}, intents: {code: name}}` from `intent_label`. Locale fallback inside the query. |
| `asoe-ui/tests/contract/test_origin_customer_label.test.ts` | `origin='CUSTOMER'` UI chrome reads "Customer Inbox" (per §4 Q7). |
| `asoe-ui/tests/hooks/test_useIntentLabel_fallback.test.ts` | Missing locale falls back to `en`; missing `en` falls back to the code itself. |
| `asoe-ui/e2e/case-classification-display.spec.ts` | Playwright: open a CUSTOMER case, assert the supergroup label rendered is the localized `display_name`, not the code. |

### 6.2 Files modified

| Path | Change |
|---|---|
| `asoe-ui/src/types/cases.ts` | **Remove** `CaseType`, `EmailClassification`, `CaseSource`. **Add** `Origin = "CUSTOMER" \| "API"`. **Update** `OrderCase` interface to mirror the new Pydantic shape: `origin`, `source_channel`, `supergroup_code: SupergroupCode`, `predecessor_case_id`, `will_miss_rdd`, `sla_tier`, `sla_due_at`. Drop `case_type` and `email_classification`. |
| `asoe-ui/src/types/exceptions.ts` | Rename type `ExceptionRecord` → `ChildCase`. Add `supergroup_code`, `divergence_reason`, `intent_code` (replaces `intent`), `sap_block_field`, `scope`. Keep `IntentCode` imported from `src/generated/taxonomy.ts`. |
| `asoe-ui/src/components/CaseList.tsx` (or equivalent — confirm during implementation) | Render `useIntentLabel(case.supergroup_code, "SUPERGROUP")` instead of the previous hardcoded mapping. Render `useIntentLabel(child.intent_code, "INTENT")` for child rows. |
| `asoe-ui/src/lib/cases.ts` (and similar API client modules) | Update field names. Drop `case_type` / `email_classification` consumers. |
| `asoe-ui/tests/contract/test_case_status_projection_parity.test.ts` | Field-name updates only. |

### 6.3 Files removed

| Path | Reason |
|---|---|
| Any UI component that hardcoded the `EMAIL_ENTRY` / `BLOCK` distinction in copy | The supergroup label replaces it. Identify during implementation via `grep "EMAIL_ENTRY\|BLOCK\|email_classification" asoe-ui/src/`. |

### 6.4 Phase 4 exit gate

```
asoe-ui$ npx vitest run
asoe-ui$ npx playwright test e2e/case-classification-display.spec.ts
```

---

## 7. Phase 5 — Agent + recipe regression (1 wk)

This phase has **no schema changes**. It's the regression bar that proves the upstream changes did not break orchestration.

### 7.1 Files modified

| Path | Change |
|---|---|
| `agents/backfill.py` | Drop any reference to `case_type` / `email_classification` introduced before this work that was preserved during Phase 2 for transition. Final state: reads only `origin` and `supergroup_code`. |
| `recipes/registry.py` | Drop any duplicate intent-string definitions. All references go through `contracts._generated.taxonomy_constants`. |
| `orchestration/nodes.py` | Same cleanup. |

### 7.2 Files added

| Path | Purpose |
|---|---|
| `tests/test_recipe_dispatch_leaf_only.py` | Synthetic test: for every active `case_intent` row, the routing table returns exactly one `queue_id`. No supergroup-keyed routing rule exists. |
| `tests/test_backfill_no_legacy_fields.py` | Grep test: the Phase-2 backfill is dead code post-cutover. (See §9 for the cleanup migration that drops the deprecated columns.) |
| `tests/test_e2e_email_to_resolution.py` | End-to-end: a CUSTOMER email arrives → case opens with `origin=CUSTOMER`, `supergroup_code=SG_<X>` → child case classified with `intent_code=INT_<Y>` → recipe dispatched → resolved → `classification_history` has 1 row per classification. Uses existing fixtures. |
| `tests/test_e2e_api_block_to_resolution.py` | Same for API path. |

### 7.3 Phase 5 exit gate

The entire backend test suite passes, **plus** the two new e2e tests:

```
pytest -q
pytest tests/test_e2e_email_to_resolution.py tests/test_e2e_api_block_to_resolution.py
```

Acceptance criteria covered after Phase 5: **#10 prerequisites in place** (Phase 6 demonstrates the timed change).

---

## 8. Phase 6 — Governance go-live (ongoing)

### 8.1 Files added

| Path | Purpose |
|---|---|
| `scripts/reconcile_sap_block_codes.py` | Connects to SAP (or reads an exported snapshot — pick during implementation), diffs `TVLST` / `TVFST` / `TVAGT` against active rows of `case_intent.sap_block_code`. Writes a steward ticket via the existing ticket mechanism for any diff. |
| `scripts/steward_change.py` | CLI: `steward_change.py add-intent --code INT_FOO --supergroup SG_BAR --sap-block-code 99`. Writes the YAML, runs the validator, opens a PR. |
| `.github/workflows/sap-block-reconciliation.yml` | Cron 02:00 UTC daily; runs the reconciliation script; opens a GitHub issue if a new code is found. |
| `docs/runbooks/taxonomy-change.md` | Steward workflow runbook per §9.1. |
| `docs/runbooks/needs-triage-aging.md` | Dashboard ops + 48h trigger. |
| `tests/test_reconcile_sap_block_codes.py` | Synthetic SAP snapshot with one new code → script produces a ticket payload with the right fields. |
| `tests/test_steward_change_workflow.py` | `steward_change.py add-intent --dry-run` produces a YAML diff that validates against the JSON-schema. |

### 8.2 Phase 6 exit gate

```
pytest tests/test_reconcile_sap_block_codes.py tests/test_steward_change_workflow.py
```

Plus, **the first real steward-driven taxonomy change ships within 3 business days end-to-end** (criterion #10). This is an operational milestone, not a test.

---

## 9. Post-cutover cleanup (separate PR, one release after Phase 5)

This is **not part of the initial implementation** but is committed in advance so it isn't forgotten.

| Path | Change |
|---|---|
| `db/migrations/V020__drop_deprecated_case_columns.sql` | Drops the generated columns `case_type`, `email_classification` from `order_case`. Drops `source` (since `origin` is the only consumer). |
| `contracts/models.py` | Delete the deprecated derivers. |
| `api/store.py`, `api/routes/cases.py` | Delete any remaining transition shims. |
| `tests/test_legacy_field_deprecation.py` | Remove (deprecation window closed). |

**Gate:** one release in production with no consumer reading the deprecated columns (verified via DB grant audit).

---

## 10. Test coverage matrix vs acceptance criteria

| Criterion (§11 of requirements) | Covered by |
|---|---|
| #1 — case creation from both origins | `test_routes_cases.py` (modified), `test_e2e_*` |
| #2 — API parent rejects divergent child | `test_case_origin_inheritance.py::test_api_strict_rejects` |
| #3 — CUSTOMER strict in v1 (divergence_reason null) | `test_case_origin_inheritance.py::test_customer_strict_v1`, `test_routes_cases.py` |
| #4 — leaf must be in supergroup_intent_allowed | `test_case_leaf_validity.py` |
| #5 — NEEDS_TRIAGE blocks RESOLVED | `test_needs_triage_close_block.py` |
| #6 — SLA per §8.4 formula | `test_sla_computation.py` (in Phase 2; minor addition not listed above — add to plan if missed) |
| #7 — routing only on leaf | `test_routing_leaf_only.py`, `test_recipe_dispatch_leaf_only.py` |
| #8 — unknown SAP block → SG_BLOCK_UNMAPPED + alert | `test_case_unmapped_block_code.py` |
| #9 — history row per classification, taxonomy-version stamped | `test_case_classification_history.py` |
| #10 — new mapping in ≤ 3 business days, no deploy | Operational; Phase 6 milestone |
| #11 — CI fails on stale taxonomy reference | `test_taxonomy_constants_drift.py` |
| #12 — backfill produces zero `SG_BLOCK_UNMAPPED` for known codes | `test_case_backfill_supergroup.py::test_no_unmapped_for_known_codes` |

**Gap noted:** criterion #6 (SLA formula). Add `tests/test_sla_computation.py` to Phase 2's file list. This is a one-line plan correction; do not start Phase 2 without it.

---

## 11. Code-smell removal scorecard

Each removed item is a deliberate consequence of the requirement, not opportunistic cleanup.

| Smell | Removed in |
|---|---|
| Static `Intent` enum (13 hardcoded values) | Phase 2 §4.2 |
| `CaseType` literal (EMAIL_ENTRY / BLOCK) duplicating `source` | Phase 2 §4.2 |
| `EmailClassification` literal as a separate axis from intent | Phase 2 §4.2 |
| `_default_case_type_from_source` validator (derived field smell) | Phase 2 §4.2 |
| `source = 'manual_order' \| 'automated_order'` — verbose, ambiguous | Phase 2 §4.2 (replaced by `origin`) |
| Health endpoint reflecting on `.__args__` (runtime introspection) | Phase 2 §4.2 (replaced by query of `case_intent`) |
| `ExceptionRecord` Python name (it's a child case, not an exception) | Phase 2 §4.2 (renamed; DB table kept) |
| Hardcoded SAP-block → intent mapping inside Python | None today, but the requirement prevents one from being written |

---

## 12. Risk register for the implementation itself (separate from the requirement's risks)

| # | Risk | Mitigation |
|---|---|---|
| IR1 | V018 backfill is large and runs inside a single transaction | Stage in a transactionally-safe pattern: add columns nullable → backfill in batches via a script that the migration calls → then `SET NOT NULL`. Local-dev seed has dozens of rows; preprod tests with prod-scale fixture. |
| IR2 | Renaming `ExceptionRecord` → `ChildCase` ripples through many files | Single PR that renames + tests; `grep -rn "ExceptionRecord" .` must be empty afterwards. CI catches stragglers. |
| IR3 | Generated constants file drift if a developer edits it by hand | The CI guard test (`test_taxonomy_constants_drift.py`) blocks the build. The file carries a `# DO NOT EDIT — generated by scripts/generate_taxonomy_constants.py` header. |
| IR4 | UI loses access to enum values during the window between Phase 2 deploy and Phase 4 deploy | Phase 2 updates the health endpoint *first*; UI continues to consume health-endpoint values during Phase 3. UI rename in Phase 4 is decoupled. |
| IR5 | The Steward DB role doesn't exist | Phase 1 V017 creates it: `CREATE ROLE ops_steward; GRANT INSERT, UPDATE, DELETE ON case_supergroup, case_intent, supergroup_intent_allowed, intent_label TO ops_steward;`. App role gets SELECT only. |
| IR6 | Reclassification audit triggers fire for *every* update including incidental ones (status changes, etc.) | Triggers are scoped: `BEFORE UPDATE OF supergroup_code ON order_case` and `BEFORE UPDATE OF intent_code ON exceptions`. Only fires when those exact columns change. |
| IR7 | NEEDS_TRIAGE close-block trapping a real production case that genuinely has no classification | Steward queue is the escape valve. A case can be reclassified by the steward at any time (§8.3). Operationally fine. |
| IR8 | `divergence_reason` column unused for the lifetime of v1 looks like dead schema | Documented in §8.1 of requirements; column comment in V018 references the spec. Acceptable. |

---

## 13. PR sequence (suggested)

| PR | Branch | Phase | Reviewable size |
|---|---|---|---|
| PR-1 | `claude/sg-phase-1-lookup-tables` | Phase 1 | ~600 LOC + seed |
| PR-2 | `claude/sg-phase-2-case-model` | Phase 2 | ~1200 LOC, **largest** |
| PR-3 | `claude/sg-phase-3-history-triage` | Phase 3 | ~500 LOC |
| PR-4 | `claude/sg-phase-4-ui-labels` | Phase 4 | ~400 LOC |
| PR-5 | `claude/sg-phase-5-regression` | Phase 5 | ~200 LOC |
| PR-6 | `claude/sg-phase-6-governance` | Phase 6 | ~400 LOC |
| PR-7 (later) | `claude/sg-cleanup-deprecated-columns` | §9 cleanup | ~100 LOC, one release later |

Each PR references the requirement section it satisfies and the acceptance criteria it closes.

---

## 14. What this plan deliberately does NOT include

- **No refactor of the recipe dispatcher.** It already obeys the routing-on-leaf rule (D7).
- **No new orchestration nodes.** Existing nodes are extended in-place.
- **No new UI page.** Steward dashboards are a follow-up (mentioned in §6.2 of the requirements but scoped out of this implementation).
- **No SLA UI changes.** SLA values are computed at create and stored; existing UI columns still render them.
- **No multi-tenant taxonomy variants.** Out of scope per requirements §2.

---

## 15. Pre-implementation checklist

Before opening PR-1:

- [ ] Phase 0 outputs received and PO-signed (§2).
- [ ] `db/seeds/case_taxonomy.yaml` committed with the seed data.
- [ ] Plan correction §10 applied: `test_sla_computation.py` added to Phase 2.
- [ ] Steward identified by name (single individual, not a committee — per requirement §9).
- [ ] Test database connection string for trigger tests confirmed (some tests cannot run on SQLite).
