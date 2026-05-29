# Demo-seed-only fixtures

Extra `*.event.json` events ingested by `scripts/seed-demo-cases.sh` to give
the deployed **All Cases** surface representational breadth matching the
asoe-ui Vercel mock catalog (`asoe-ui/src/lib/mock-data/`).

These live here — **not** in `tests/fixtures/synthetic/` — because that
directory is the assertion set for `scripts/smoke-e2e.sh`, which globs every
`*.event.json` and asserts a deterministic `intent` + `selected_recipe`.
Events here intentionally land on paths the smoke set excludes:

- `mass_pricing_error.event.json` — `line_count > 10` →
  deterministic `MASS_PRICING_ERROR`, which has **no mapped recipe** by
  design (`tests/fixtures/synthetic/README.md`) and routes to
  `FAIL_TO_HUMAN`. That `FAILED` terminal IS representational — it mirrors
  the mock's failed mass-pricing cases (`exc-022` / `exc-023`) — but it
  would break the smoke assertions (no `selected_recipe`), so it is
  seed-only.

Unlike the synthetic smoke fixtures there are no `*.expected.json` sidecars:
the seed script does not assert outcomes, it only ingests for display.

To add a scenario, drop a `<name>.event.json` here. It must be a valid
`ResolveRequest` payload; carry `metadata.synthetic = true` and
`metadata.source = "seed-demo-cases"` so the audit chain can distinguish
demo traffic from real ingestion.

## Durability caveat

Seeded cases are **not durable**. OrderCases live only in the in-memory
`CaseStore` (DB-backed persistence is deferred to Phase H.7), so they are
lost on every container restart/scale event and are per-replica. Pin
`maxReplicas=1` for the demo window and re-seed after any restart. See
the header of `scripts/seed-demo-cases.sh` for the full explanation.
