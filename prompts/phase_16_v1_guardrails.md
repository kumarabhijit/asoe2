# Phase 16 — V1 Foundation Guardrail Tests (CI Enforcement)

```text
Read architecture_v3.md §15 (V1 Foundation Guardrails), CLAUDE.md, DESIGN.md,
and tasks.md (Phase 16).
Implement only Phase 16.

Requirements:

Implement CI-automated tests for all 6 V1 Foundation Guardrails from §15.
These tests must fail the build if a guardrail is violated — they are not
optional code review suggestions.

1. Guardrail #1 — No intent-specific logic in pipeline nodes:
   - AST inspection of orchestration/nodes.py
   - Verify no Compare nodes test against intent string literals
     ("CONTRACTUAL_CORRECTION", "CREDIT_BLOCK", "MASS_PRICING_ERROR", "DUPLICATE_PO")
   - Belt-and-suspenders grep: quoted intent strings must not appear in nodes.py
   - Sanity check: expected pipeline functions exist

2. Guardrail #2 — Dynamic enum serving:
   - GET /api/v1/health must serve allowed_intents matching AllowedIntent.__args__
   - GET /api/v1/health must serve allowed_recipes matching AllowedRecipeName.__args__
   - 11 lifecycle states per §9.1
   - Verify the health route imports from specs, not hardcoded lists

3. Guardrail #3 — Metadata keys documented per RecipeSpec:
   - Add expected_metadata_keys field to RecipeSpec (tuple[str, ...], default empty)
   - DuplicatePORecipe declares ("signal_scores", "matched_po_id")
   - All RecipeSpecs have the field (even if empty)
   - Test fixtures in conftest.py include all declared metadata keys

4. Guardrail #4 — ERP-agnostic gateway protocol:
   - Scan gateways/base.py, executor.py code (excluding comments/docstrings)
     for ERP-specific terms: BAPI, RFC, IDOC, SAP, Oracle, Dynamics, YK07, etc.
   - Verify GatewayRequest/GatewayResponse field names contain no ERP terms

5. Guardrail #5 — Intent-agnostic exceptions table schema:
   - Introspect exceptions table columns from SQLite migration
   - No column matches intent-specific patterns: similarity, damage, deduction, etc.
   - resolution_data column exists (the only extensibility point)
   - Scan PostgreSQL migration SQL for the same

6. Guardrail #6 — Hierarchical policy key format:
   - Regex validation: ^(global|tenant\.{id}|retailer\.{id}(\.category\.{cat})?)\.\w+$
   - Valid keys accepted, flat keys rejected
   - Existing test_api.py policy keys follow the format
   - Database repository writes produce valid keys

Additionally:
- Invariant #11: recipes never import from contracts/policy (AST-based check)

Constraints:
- AST inspection for code checks, not string matching on comments/docstrings
- Tests must be self-contained — no external dependencies beyond the codebase
- All tests use existing test infrastructure (pytest, SQLite in-memory)
- Do not modify orchestration/nodes.py or gateways/ — these tests verify existing code

Add tests in: tests/test_v1_guardrails.py

Update: DESIGN.md (test coverage table), tasks.md (Phase 16 checklist),
README.md (phase table, test count), prompts/full_project_sequence.md,
recipes/registry.py (expected_metadata_keys), tests/conftest.py (add matched_po_id).
```
