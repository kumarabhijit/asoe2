# Prompt: Convert a Product Owner Specification to ASOE Architecture

```text
You are implementing a new capability into the ASOE Skill–Shadow–Recipe system.
The Product Owner has provided a specification file. Your job is to convert it
into the correct architectural components — not to execute it as-is.

---

## MANDATORY PRE-FLIGHT READS

Before touching any file, read all of these in full:
1. CLAUDE.md                          — guardrails and working rules
2. architecture_v2.md                 — system design
3. DESIGN.md                          — implementation details (file names, class names, wiring)
4. contracts/models.py                — current Intent enum and all Pydantic models
5. constraints/specs.py               — current AllowedIntent, AllowedRecipeName literals
6. constraints/guidance_backend.py    — current constrained-generation regex patterns
7. recipes/registry.py                — currently registered recipes
8. skills/loader.py                   — current skill routing logic
9. orchestration/nodes.py             — current validate_types branches
10. The PO specification file itself   — read it completely before forming any plan

---

## STEP 0 — SPEC ANALYSIS GATE (do this before writing a single line of code)

Classify every section of the PO spec into one of three buckets:

  SKILL territory (guide reasoning only):
    - intent name and description
    - which recipe to call for which intent
    - compliance/shadow protocol reminder
    - constrained generation policy statement
    → Goes into: skills/<name>_SKILL.md

  RECIPE territory (deterministic execution logic):
    - scoring algorithms, weights, thresholds
    - decision trees and branching conditions
    - field validation and business rules
    - action/status mappings
    → Goes into: recipes/<Name>Recipe.py

  REFERENCE / PRODUCT SPEC territory (not runtime code):
    - API endpoint designs
    - database schemas / DDL
    - UX wireframes and UI patterns
    - integration diagrams and middleware patterns
    - tech stack recommendations
    - testing strategy descriptions
    - metrics targets and SLAs
    → Goes into: docs/specs/<name>-product-spec.md (move or copy; never into skills/)

If the spec mixes all three (as PO specs typically do), explicitly list which
sections map to which bucket before writing any code.

HALT CONDITION: If the recipe logic requires calling an external service,
querying a database, or producing non-deterministic outputs, stop and request
a new infrastructure recipe or human escalation path. Do not inline I/O into
a recipe.

---

## STEP 1 — NAME THE INTENT

Choose one new intent name (SCREAMING_SNAKE_CASE) that will appear:
- as a new value in contracts/models.py Intent enum
- as a new value in constraints/specs.py AllowedIntent Literal
- as a new entry in constraints/guidance_backend.py intent_regex()

Rules:
- The intent must be a classification label, not a verb or an action.
  Good: DUPLICATE_PO, PROMO_PRICE_VARIANCE
  Bad:  BLOCK_DUPLICATE, RUN_DEDUP_CHECK
- If the spec covers more than one distinct intent, define one intent per
  distinct recipe. Do not bundle multiple intents into a single recipe.

---

## STEP 2 — WRITE THE RECIPE FIRST

File: recipes/<IntentName>Recipe.py

Rules:
- One pure Python function per recipe.
- Function signature: explicit typed parameters — no **kwargs, no GraphState.
- All business logic (weights, thresholds, conditions) lives here and nowhere else.
- Return type: Dict[str, Any] with a required "status" key.
  Status values must be one of: "SUCCESS" | "BLOCKED" | "REJECTED" | "FAILED"
  plus any domain-specific classification values (e.g., "REVIEW_REQUIRED").
- No LLM calls, no I/O, no side effects, no imports beyond stdlib and typing.
- Include a module-level assertion for any invariant that must hold at load time
  (e.g., weights summing to 1.0).
- Do not duplicate or reference compliance logic — that is the Shadow's job.

Parameter sourcing (where will these values come from at runtime?):
  - Scalars already on OrderEvent (order_id, retailer_id, po_price, etc.)
    → pass directly from state.event.*
  - Computed values or nested structures not on OrderEvent
    → pass via state.event.metadata["key"] (the metadata dict is the escape hatch)
  - Do not add new top-level fields to GraphState or OrderEvent unless the
    data cannot fit in metadata and is genuinely first-class to the domain.

---

## STEP 3 — UPDATE THE VOCABULARY (all four locations, in this order)

Each new intent and recipe name must be added to all four locations.
Drift between them breaks the constrained-generation guarantee.

  a. contracts/models.py         — add to Intent enum
  b. constraints/specs.py        — add to AllowedIntent Literal
                                   add to AllowedRecipeName Literal
  c. constraints/guidance_backend.py — extend intent_regex() pipe-separated string
                                       extend recipe_name_regex() pipe-separated string
  d. recipes/registry.py         — add RecipeSpec entry with:
                                     name, func, required_params, allowed_intents

After updating all four, verify they are in sync:
  set(AllowedIntent.__args__) must match the non-UNKNOWN Intent enum values
  set(AllowedRecipeName.__args__) must match set(REGISTRY.keys())
  intent_regex() must fullmatch every AllowedIntent value
  recipe_name_regex() must fullmatch every AllowedRecipeName value

---

## STEP 4 — WRITE THE SKILL FILE

File: skills/<intent-kebab-case>_SKILL.md

Required frontmatter fields:
  name, description, metadata.version, metadata.recipes, metadata.constrained_generation

Required sections (keep each to 3–8 lines):
  1. Overview       — one paragraph, what event triggers this skill
  2. Reasoning Loop — numbered steps; ends with "Do not improvise the logic."
  3. Constrained Generation Policy — list machine-consumed outputs and their schemas
  4. Recipe-to-Intent Mapping — one line per intent → recipe (or FAIL_TO_HUMAN)
  5. Execution Protocol — Compliance Shadow check before any recipe call
  6. Output Requirements — what the execution log must capture for audit

Must NOT contain: weights, thresholds, decision trees, SQL, API specs, UX,
  tech stack, integration diagrams, or any content from RECIPE or REFERENCE buckets.

---

## STEP 5 — WIRE THE ORCHESTRATION (two files)

  a. skills/loader.py — select_for_event()
     Add an elif branch before the generic EDI_850 catch-all.
     Match on the event_type string that will appear in OrderEvent.event_type.
     Example: if "DUPLICATE" in upper: return self.load_by_name("duplicate-po_SKILL.md")

  b. orchestration/nodes.py — validate_types()
     Add an elif branch for the new recipe name.
     Build RecipeInvocation.params from state.event fields and state.event.metadata.
     Do not compute any business logic here — only extract and map fields.

---

## STEP 6 — MOVE THE SPEC

Move (do not copy) the PO's original specification file to:
  docs/specs/<descriptive-name>-product-spec.md

Use git mv (or equivalent) so history is preserved.
The file must not remain in skills/ — the skill loader will pick it up otherwise.

---

## STEP 7 — WRITE TESTS

New test classes required (add to existing test files, do not create new files
unless a new file is clearly warranted):

  tests/test_recipes.py — class Test<IntentName>Recipe
    Cover at minimum:
      - Each classification band / status (one test per distinct output path)
      - Every threshold boundary (closed lower bound)
      - Missing or empty optional inputs (must not raise)
      - Output dict contains all required keys
      - Inputs are echoed correctly where applicable

  tests/test_registry.py
    - Update the exact-count assertion (e.g., == 2 → == 3)
    - Add class Test<IntentName>Spec covering: name, required_params,
      allowed_intents, func callable, frozen immutability

  tests/test_contracts.py
    - Add the new Intent value to test_intent_values expected set

  tests/test_constraints.py
    - Update test_intent_regex_is_correct expected string
    - Update test_recipe_name_regex_is_correct expected string

  tests/test_executor.py
    - Update test_registered_names_returns_* count and membership assertions

---

## STEP 8 — RUN TESTS AND CONFIRM

Run: python -m pytest

All tests must pass before committing.
If any test fails, fix the root cause — do not suppress or skip the test.

---

## STEP 9 — COMMIT

Stage only the files changed by this task. Commit message format:

  feat: add <INTENT_NAME> intent, <Name>Recipe, and <intent> skill

  - recipes/<Name>Recipe.py: <one-line description of what the recipe does>
  - contracts/models.py: add <INTENT_NAME> to Intent enum
  - constraints/specs.py: add to AllowedIntent and AllowedRecipeName
  - constraints/guidance_backend.py: extend intent and recipe regexes
  - skills/<intent>_SKILL.md: minimal ASOE playbook
  - recipes/registry.py: register <Name>Recipe.py
  - skills/loader.py: route <EVENT_TYPE> to <intent> skill
  - orchestration/nodes.py: add validate_types branch for <Name>Recipe.py
  - docs/specs/<name>-product-spec.md: PO spec preserved as reference
  - tests: <N> new test cases; vocabulary sync assertions updated

  All <N> tests pass.

---

## HALT CONDITIONS — stop and ask before proceeding if:

1. The spec's "recipe logic" requires calling an external API, database, or
   message queue — that is infrastructure, not a recipe.
2. The spec defines more than one autonomy tier / escalation path and it is
   unclear which maps to GREEN / YELLOW / RED compliance verdicts.
3. The spec's intent cannot be classified to a single AllowedIntent value
   (e.g., it is a multi-step workflow spanning multiple existing intents).
4. A new field is needed on GraphState that cannot fit in OrderEvent.metadata.
5. The spec describes a feedback / retraining loop — that is out of scope for
   the deterministic recipe layer.
6. Any section of the spec is ambiguous about whether a threshold or weight
   is fixed policy or tenant-configurable runtime config.

In all halt cases, output:
  HALT — <one-sentence reason>
  Question for architect: <specific question>
  Do not proceed until answered.
```
