# Synthetic E2E smoke fixtures

One pair per intent driven through the full Skill→Shadow→Recipe pipeline by
`scripts/smoke-e2e.sh`:

- `<intent>.event.json` — `ResolveRequest` payload sent to
  `POST /api/v1/exceptions/resolve`. Mirrors the canonical event shape
  used by the corresponding `tests/test_e2e_*.py` integration tests so the
  smoke loop is exercising the same input space the unit tests cover.
- `<intent>.expected.json` — assertions on the response. Fields:
  - `intent` (str): exact value the classifier must return.
  - `selected_recipe` (str): exact value from the recipe registry.
  - `allowed_shadow_verdicts` (list[str]): set the verdict must fall into.
  - `allowed_final_statuses` (list[str]): set the final status must fall into.

Why `allowed_*` lists rather than a single value: the deterministic part
of the contract is the intent classification + recipe selection. The
shadow verdict and final status legitimately depend on policy thresholds
(e.g. high-value cosign, severity-graded YELLOW vs RED). Failing the
smoke when an event happens to land in a different policy bucket would
be a flake, not a bug — so we constrain to the closed set and let the
exact value vary.

`MASS_PRICING_ERROR` is intentionally not represented here: it has no
mapped recipe (routes to FAIL_TO_HUMAN by design), so it would break the
smoke `selected_recipe` assertion. The `BLOCKED` path is exercised via
the YELLOW/RED branches of other intents.

Seeding vs smoke: `scripts/seed-demo-cases.sh` reuses these same fixtures
(plus demo-only extras under `scripts/seed-fixtures/`, including the
`MASS_PRICING_ERROR` FAILED-case representation) to POPULATE a deployed
All Cases surface for parity with the asoe-ui Vercel mock. That seeder
never asserts; this directory stays the pass/fail contract set for
`scripts/smoke-e2e.sh`.

Synthetic vs production data: every event carries `metadata.synthetic`
and `metadata.source = "smoke-e2e"` so the audit chain can distinguish
test traffic from real ingestion.
