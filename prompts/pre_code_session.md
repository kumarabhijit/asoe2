# Pre-Code Session — General Guidance

```text
You are about to make a code change to the ASOE codebase. Before writing or
modifying any file, complete the pre-flight checklist and orientation below.
This prompt applies to all code changes — new features, bug fixes, refactors,
and test additions.

---

## MANDATORY PRE-FLIGHT READS

Read these documents in full before touching any source file:

1. CLAUDE.md                   — architecture guardrails, engineering rules, definition of done
2. architecture_v2.md          — Skill-Shadow-Recipe design, execution invariants, technology stack
3. DESIGN.md                   — module map, class/function names, graph node wiring, env vars
4. tasks.md                    — phase checklist, current status, open items

Then read every file you intend to modify so you understand its current state,
conventions, and test coverage.

---

## ORIENTATION — KNOW WHERE YOU ARE

Before planning your change, answer these questions (to yourself, not aloud):

1. Which layer does this change belong to?
   - contracts/      — Pydantic models, typed state, policy constants
   - skills/         — SKILL.md files (reasoning guidance only, no execution logic)
   - recipes/        — deterministic business logic (pure functions, no I/O)
   - compliance/     — Compliance Shadow (audit + enforce, no recipe selection)
   - constraints/    — constrained-generation schemas, backends, router
   - orchestration/  — LangGraph state machine, node functions, circuit breaker
   - gateways/       — infrastructure gateway layer (Ports & Adapters)
   - workflows/      — multi-step workflow runner (Saga pattern)
   - hardening/      — kill switch, explain mode
   - observability/  — structured tracing (TraceRecord)
   - tests/          — pytest suite

2. Does this change cross a layer boundary?
   If yes, identify every file on both sides and plan the change as a
   coordinated set. Do not leave layers out of sync.

3. Does this change touch a constrained vocabulary?
   If yes, update all four sync points (see README § "Defining and enforcing
   constraints"):
   - contracts/models.py (Intent enum)
   - constraints/specs.py (Literal types)
   - constraints/guidance_backend.py (regex patterns)
   - constraints/fallback_backend.py (classification branches)

---

## ARCHITECTURAL INVARIANTS — DO NOT VIOLATE

These are enforced by code and tests. Any change that breaks them is wrong.

1.  No recipe runs unless Compliance Shadow verdict is GREEN.
2.  No recipe runs unless the recipe name is in the allowed set.
3.  No recipe runs unless all required parameters are non-null.
4.  Compliance trace_id propagates to execution log unchanged.
5.  GraphState forbids untyped fields (extra="forbid").
6.  Kill switch check precedes all node execution.
7.  Explain mode suppresses only recipe execution; shadow always runs.
8.  Recipe executor has no audit, enforce, or classify methods.
9.  Skill definitions are loaded verbatim — no summarisation or rewriting.
10. All constrained outputs are validated by Pydantic before state advances.
11. Recipes never import from the policy module — thresholds are injected by
    the orchestration layer.

If your change would require violating any invariant, HALT and request
architectural clarification.

---

## CHANGE PLANNING CHECKLIST

Before writing code, confirm each item:

- [ ] I have identified the exact files I will modify.
- [ ] I have read each of those files in their current state.
- [ ] I know which tests cover the code I am changing.
- [ ] My change does not introduce execution logic into a SKILL file.
- [ ] My change does not introduce business logic into orchestration nodes.
- [ ] My change does not bypass or weaken Compliance Shadow gating.
- [ ] My change does not add untyped fields to GraphState.
- [ ] My change does not hardcode thresholds outside contracts/policy.py.
- [ ] My change does not add speculative features beyond the stated task.
- [ ] If I am adding a new recipe, I am following prompts/po-spec-to-asoe.md.
- [ ] If I am updating docs, I am following prompts/update_docs.md.

---

## IMPLEMENTATION RULES

### Scope
- Make the smallest viable increment. One concern per change.
- Do not refactor, rename, or "improve" code outside the scope of the task.
- Do not add comments, docstrings, or type annotations to unchanged code.
- Do not add error handling for scenarios that cannot happen.
- Three similar lines of code is better than a premature abstraction.

### Code style
- Small modules, typed state, explicit contracts, pure functions where practical.
- Narrow interfaces, readable code, no hidden side effects.
- No dynamic metaprogramming unless clearly justified.
- Constrained generation is mandatory for any LLM output consumed by code.
- Free-form text is allowed only for human-facing explanation fields.

### State and contracts
- All new Pydantic models use extra="forbid".
- Keep separate: inbound event data, decision state, compliance result,
  recipe output, final response.
- Do not overload fields with mixed meanings.

### Node design
- Each LangGraph node does one clear job.
- Reads current state, returns a partial state update.
- Must not silently swallow failures or mutate hidden state.

---

## TESTING RULES

- Write or update tests with every code change.
- Tests go in the existing test file for the module (do not create new test
  files unless clearly warranted).
- Test both the happy path and the failure/edge paths.
- Make test failures specific and actionable (assert on exact values, not
  truthiness).
- No flaky tests, no timing dependencies, no network calls in CI.
- Run python -m pytest after every change. All tests must pass before commit.

---

## COMMIT RULES

- Stage only the files changed by this task.
- Commit message format: "<type>: <concise description>"
  Types: feat, fix, refactor, test, docs, chore
- If the change spans multiple concerns, prefer multiple small commits over
  one large commit.
- Do not commit files containing secrets (.env, credentials, API keys).

---

## HALT CONDITIONS — STOP AND ASK IF:

1. The deterministic execution path is unclear or incomplete.
2. Required execution logic does not exist as a recipe.
3. A threshold, permission, or policy value needs to be invented.
4. The change requires violating an architectural invariant.
5. A new field is needed on GraphState that cannot fit in OrderEvent.metadata.
6. The change requires calling an external service from within a recipe.
7. The constrained-generation path cannot reliably produce the required output.
8. You are unsure whether something belongs in a skill, recipe, or
   orchestration node.

In all halt cases, output:
  HALT — <one-sentence reason>
  Question for architect: <specific question>
  Do not proceed until answered.

---

## POST-CHANGE VERIFICATION

After completing the change:

1. Run python -m pytest — all tests must pass.
2. Verify no constrained-vocabulary drift (AllowedIntent, AllowedRecipeName,
   Intent enum, regex patterns must stay in sync).
3. Verify no recipe imports from contracts/policy.py.
4. Verify GraphState still rejects unknown fields.
5. Confirm the change is small and reviewable.
6. Confirm docs are updated if the change affects user-facing behaviour
   (use prompts/update_docs.md).

Return: a concise summary of what was changed, files touched, tests
added/modified, and confirmation that all tests pass.
```
