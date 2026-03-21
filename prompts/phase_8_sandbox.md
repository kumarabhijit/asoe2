# Phase 8 — Local Execution Sandbox

```text
Read architecture_v2.md, DESIGN.md, CLAUDE.md, tasks.md (Phase 8), and
tests/sandbox/ui/app.py before making any changes.

Goal: provide an interactive, local execution environment that lets engineers
and auditors run live pipeline events without cloud dependencies or production
system access.

Phase 8 components:
1. SQLite seeder (tests/sandbox/seed.py)
   - Creates sandbox.db with sample SAP pricing, retailer contracts,
     credit profiles, and EDI events covering all four intents.
   - CLI: python tests/sandbox/seed.py [--db <path>] [--reset]

2. Streamlit UI (tests/sandbox/ui/app.py)
   - Sidebar: pick a seeded event or enter a custom one.
   - Runs full run_graph() pipeline.
   - Displays step-by-step execution trace, shadow detail, SKILL.md viewer,
     Prompt Preview, full JSON GraphState, and gateway activity.
   - Launch: PYTHONPATH=. streamlit run tests/sandbox/ui/app.py

3. LocalHFBackend (tests/sandbox/llm/local_backend.py)
   - Implements classify_intent / propose_recipe / shadow_decision.
   - Uses Outlines constrained-JSON generation with a local HuggingFace model.
   - Gracefully falls back to DeterministicFallbackBackend on any load error.
   - Injected via LOCAL_LLM_BACKEND_CLASS env var.

4. Prompt templates (tests/sandbox/llm/prompts.py)
   - Standalone functions that build intent, recipe, and shadow prompts
     from raw event dicts (used by the UI Prompt Preview expander).

Constraints:
- Sandbox code must not alter contracts/, orchestration/, compliance/,
  recipes/, or any other production module.
- sandbox.db must be git-ignored; only seed.py is committed.
- Sandbox deps are isolated in tests/sandbox/requirements-sandbox.txt.
- ASOE_EXPLAIN_MODE and ASOE_KILL_SWITCH must be honoured by the sandbox UI.

Return: a concise summary of what was added or changed, and confirmation
that python -m pytest (the CI suite) still passes unchanged.
```
