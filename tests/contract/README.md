# tests/contract — Spec-as-Oracle Tests

Spec-anchored contract tests that fail when the implementation drifts from a
machine-readable specification.

Reference: `docs/test-strategy/design.md`, `docs/test-strategy/eng-review-test-plan.md`.

## The pattern

Every test in this directory reads from one of these spec sources and asserts
that the implementation matches:

| Spec source                                           | Owner role        |
|-------------------------------------------------------|-------------------|
| `compliance/audit_bearing_registry.yaml`              | Compliance        |
| `constraints/specs.py` (Literal vocabularies)         | Architecture      |
| `openapi/asoe2.openapi.json` (regenerated artifact)   | API contract      |
| `recipes/registry.py` (recipe registry)               | Skill–Shadow–Recipe |
| `contracts/models.py` (TerminalStatus, lifecycle)     | State machine     |

Tests under this directory must NOT assert "implementation == implementation"
(e.g. compare two hand-written constants in the same repo). They must assert
"implementation == spec" — the spec being one of the artifacts above.

## Files

- `test_intent_recipe_parity.py` — every `AllowedIntent` has a registered recipe
  and a skill file; every recipe in `AllowedRecipeName` is in
  `recipes.registry.REGISTRY`.
- `test_override_tag_casing.py` — every per-intent override-reason vocabulary
  follows the documented ADR-033 casing convention.
- `test_stub_schema_conformance.py` — every `StubGateway` response in
  `api/sandbox_gateways.py` and `tests/conftest.py` carries a payload whose
  shape matches the corresponding gateway response model.
- `test_audit_context_missing_routing.py` — `AUDIT_CONTEXT_MISSING` terminal
  status routes to `FAILED` lifecycle and its enum value is exposed via
  `/api/v1/health`.
- `test_recipe_invariants.py` — property-based: for every registered recipe,
  random `OrderEvent` instances produce valid recipe outputs.
- `test_workflow_pipeline_invariants.py` — for every (intent, recipe) pair in
  `INTENT_TO_RECIPE_NAME`, the full LangGraph pipeline produces a `final_status`
  in `LIFECYCLE_STATES` and a populated `ExecutionLog`.
- `test_multi_step_workflow_pipeline.py` — multi-step Saga workflows: chained
  intents run end-to-end, cross-step state is preserved, and compensation
  recipes run on failure.

## Adding a new contract test

1. Pick a spec source from the table above.
2. Read the spec at test-collection time (module top-level, not inside a fixture).
3. Parametrise on the spec entries so adding a new entry to the spec
   automatically expands coverage.
4. Fail with a structured message that names which spec entry leaks
   (e.g. "`PriceHoldAnalysisData.foo` is registry-required but no recipe
   path populates it"), not a bare assertion error.
