# Phase 11 — Duplicate PO Product Spec Gap Closure

```text
Read architecture_v2.md, DESIGN.md, CLAUDE.md, tasks.md (Phase 11), and
docs/specs/duplicate-po-product-spec.md in full before making any changes.

This phase closes the gaps between the Duplicate PO product specification
and the ASOE codebase.  All changes follow the Skill–Shadow–Recipe
architecture — ASOE is an AI-assisted exception resolution agent, not an
OMS or ERP system.

---

## SCOPE

The product specification (docs/specs/duplicate-po-product-spec.md) describes
a full-stack Duplicate PO platform.  Phase 11 implements only the resolution
intelligence that belongs inside ASOE:

  IN SCOPE (ASOE exception resolution layer):
    - All 6 resolution actions constrained as AllowedResolutionAction
    - Resolution decision tree inside the recipe (pure function)
    - Gateway dependencies for resolution context (resolved before recipe)
    - Autonomy-level policy mapping (L1–L4 → terminal status routing)
    - Override audit fields for human resolution tracking
    - Buyer notification via gateway effects

  OUT OF SCOPE (OMS/ERP/platform concerns):
    - Signal computation service (upstream EDI pipeline)
    - PO normalization, candidate retrieval, lookback windows
    - Data model (incoming_po, duplicate_check_results tables)
    - REST API endpoints
    - Exception queue UI
    - Per-tenant configuration UI
    - Feedback loop / retraining

---

## PHASE 11.1 — Resolution Actions (AllowedResolutionAction)

Add a 5th constrained vocabulary: AllowedResolutionAction.

Allowed values:
  BLOCK_AND_NOTIFY, MERGE, SUPERSEDE, ALLOW_BOTH,
  ESCALATE, REQUEST_BUYER_CONFIRMATION

Sync across 5 locations:
  a. constraints/specs.py          — AllowedResolutionAction Literal
  b. constraints/guidance_backend.py — resolution_action_regex()
  c. recipes/DuplicatePORecipe.py  — _DEFAULT_ACTIONS mapping
  d. recipes/DuplicatePORecipe.py  — _NOTIFICATION_TEMPLATES mapping
  e. tests/test_constraints.py     — vocab sync assertions

This is the first vocabulary that lives in the recipe output rather than
the constraint backend.  It constrains the recommended_action field returned
by DuplicatePORecipe.

---

## PHASE 11.2 — Gateway Dependencies for Resolution Context

Declare OMS gateway dependencies on DuplicatePORecipe:

  dependencies:
    oms/get_fulfillment_status  → state.resolved_data["fulfillment_status"]
    oms/get_matched_po_details  → state.resolved_data["matched_po_details"]

validate_types extracts and injects into recipe params:
    original_fulfilled      ← fulfillment_status.fulfilled
    has_revision_indicator  ← matched_po_details.has_revision_indicator
    line_items_identical    ← matched_po_details.line_items_identical

Recipe signature adds these as Optional[bool] = None for backward
compatibility.  When None, the decision tree falls back to default actions.

Key rule: gateways resolve FACTS, recipes apply RULES to facts.
The decision tree logic lives in the recipe, not in orchestration.

---

## PHASE 11.3 — Resolution Decision Tree

Add _resolve_action() to DuplicatePORecipe.py — a pure function mapping
(classification + resolution context) to one of the 6 actions.

Decision tree (spec §3.2):

  AUTO_BLOCK tier:
    identical lines + not fulfilled   → BLOCK_AND_NOTIFY  (true duplicate)
    identical lines + fulfilled       → ALLOW_BOTH        (likely reorder)
    revision indicator present        → SUPERSEDE
    lines differ, no revision         → MERGE

  REVIEW_REQUIRED tier:
    revision indicator present        → SUPERSEDE
    identical lines                   → ESCALATE
    lines differ                      → REQUEST_BUYER_CONFIRMATION

  SOFT_FLAG / PASS: use default actions (low confidence, context irrelevant)

  No context (all None): fall back to _DEFAULT_ACTIONS per classification.

---

## PHASE 11.4 — Autonomy-Level Policy Mapping

Add DUPLICATE_PO_AUTONOMY_LEVELS to contracts/policy.py:

  BLOCK_AND_NOTIFY        → L3  (act & inform)
  MERGE                   → L2  (recommend, human approves)
  SUPERSEDE               → L2
  ALLOW_BOTH              → L3
  ESCALATE                → L1  (observe only)
  REQUEST_BUYER_CONFIRMATION → L2

Recipe includes autonomy_level in output.

execute_recipe node routing:
  L1/L2 → MANUAL_REVIEW_REQUIRED (human approval required)
  L3/L4 → proceed to normal status routing (COMPLETE / BLOCKED / etc.)

Autonomy check runs BEFORE recipe status routing — L2 action with
status=BLOCKED still routes to MANUAL_REVIEW_REQUIRED, not BLOCKED.

---

## PHASE 11.5 — Override Audit Fields

Add to ExecutionLog:
  resolved_by:       Optional[str]  — identity of human who resolved
  resolved_action:   Optional[str]  — actual action taken (may differ from agent recommendation)
  resolution_notes:  Optional[str]  — human-provided reason for override

Add matching fields to TraceRecord.  Tracer.build_record() extracts them
from state.execution_log.  All fields default to None (agent auto-executed).

These fields are populated by the caller (API, UI, workflow runner) when a
human overrides the agent's recommended_action.  ASOE does not enforce
who can override — that is an API/RBAC concern.

---

## PHASE 11.6 — Buyer Notification Gateway Effect

Recipe output includes notification_template:
  BLOCK_AND_NOTIFY           → "duplicate_po_blocked"
  MERGE / SUPERSEDE          → "duplicate_po_amended"
  REQUEST_BUYER_CONFIRMATION → "duplicate_po_inquiry"
  ALLOW_BOTH / ESCALATE      → None (no notification)

DuplicatePORecipe registry declares:
  effects=(
      GatewayEffect(
          gateway_name="buyer_notification",
          operation="send",
          params_from_output={
              "template": "notification_template",
              "po_number": "incoming_po_number",
              "customer_id": "customer_id",
          },
      ),
  )

apply_effects node dispatches the notification after recipe execution.
Template rendering is the gateway adapter's responsibility — ASOE declares
what template and what data, not how to render or deliver.

---

## PHASE 11.7 — Tests

44 new tests (540 → 584 total):

  test_recipes.py:
    TestDuplicatePODecisionTree     — 11 tests (one per leaf + defaults)
    TestDuplicatePOAutonomyLevel    — 5 tests
    TestDuplicatePONotificationTemplate — 6 tests

  test_nodes.py:
    TestDuplicatePOValidateTypesNode — 2 new (resolution context injection)
    TestDuplicatePOAutonomyRouting   — 3 tests (L1/L2/L3 routing)

  test_constraints.py:
    TestGuidanceRegexBackend         — 2 new (resolution action regex)
    TestVocabularySyncGuidanceToLiterals — 2 new (resolution action sync)

  test_registry.py:
    TestDuplicatePOSpec              — 4 new (gateway deps + effects)

  test_observability.py:
    TestOverrideAuditFields          — 7 tests

  test_graph_paths.py:
    TestDuplicatePOPath              — 2 new (notification effect e2e)

  conftest.py:
    _register_oms_stub fixture (autouse) — registers OMS + buyer_notification stubs

---

## CONSTRAINED VOCABULARY SYNC — 5 POINTS

After Phase 11, there are 5 constrained vocabularies that must stay in sync:

  1. AllowedIntent:
     contracts/models.py (Intent enum) ↔ constraints/specs.py ↔
     constraints/guidance_backend.py ↔ constraints/fallback_backend.py

  2. AllowedShadowStatus:
     constraints/specs.py ↔ constraints/guidance_backend.py ↔
     constraints/fallback_backend.py

  3. AllowedRecipeName:
     constraints/specs.py ↔ constraints/guidance_backend.py ↔
     recipes/registry.py

  4. AllowedResolutionAction:
     constraints/specs.py ↔ constraints/guidance_backend.py ↔
     recipes/DuplicatePORecipe.py (_DEFAULT_ACTIONS + _NOTIFICATION_TEMPLATES)

  5. DUPLICATE_PO_AUTONOMY_LEVELS:
     contracts/policy.py ↔ recipes/DuplicatePORecipe.py (via injection)

---

## ARCHITECTURAL INVARIANTS — PRESERVED

  - Recipe remains a pure function: no I/O, no policy imports
  - Resolution context resolved via gateway dependencies before execution
  - Decision tree logic inside recipe, not in orchestration
  - Autonomy routing uses existing terminal statuses, no new abstractions
  - Notifications use existing GatewayEffect pattern
  - Override audit is additive (Optional fields, no breaking changes)
  - All 11 execution invariants from architecture_v2.md §5 remain intact
```
