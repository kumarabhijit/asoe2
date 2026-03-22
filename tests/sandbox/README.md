# ASOE Sandbox

Local execution environment for testing the full Skill–Shadow–Recipe pipeline
without any cloud services.  Uses a SQLite database for seed data and
(optionally) a local open-weights model for constrained LLM generation.

```
tests/sandbox/
├── db/
│   └── schema.sql          ← authoritative schema reference
├── llm/
│   ├── __init__.py
│   ├── local_backend.py    ← LocalHFBackend (Outlines + HuggingFace)
│   └── prompts.py          ← human-readable prompt templates (used by UI)
├── ui/
│   ├── __init__.py
│   └── app.py              ← Streamlit trace visualiser
├── seed.py                 ← SQLite seeder (generates sandbox.db)
├── requirements-sandbox.txt
├── .gitignore
└── README.md               ← this file
```

> `sandbox.db` is gitignored — only `seed.py` is committed.

---

## Quick start

### 1. Install sandbox dependencies

```bash
pip install -r tests/sandbox/requirements-sandbox.txt
```

### 2. Seed the database

```bash
python tests/sandbox/seed.py
# → tests/sandbox/sandbox.db  (8 EDI events covering all 4 intents)

# Force recreate:
python tests/sandbox/seed.py --reset

# Custom path:
python tests/sandbox/seed.py --db /tmp/my_test.db
```

### 3. Launch the UI

```bash
cd /path/to/asoe
PYTHONPATH=. streamlit run tests/sandbox/ui/app.py
```

The UI opens at **http://localhost:8501**.

---

## Seeded events

| Event ID      | Intent                  | Order   | Notes                          |
|---------------|-------------------------|---------|-------------------------------|
| EVT-CC-001    | CONTRACTUAL_CORRECTION  | SO-1001 | 10 % discount — within 15 %   |
| EVT-CC-002    | CONTRACTUAL_CORRECTION  | SO-1002 | 8 % discount — within 15 %    |
| EVT-CC-003    | CONTRACTUAL_CORRECTION  | SO-1003 | 22 % discount — exceeds threshold → FAILED |
| EVT-CB-001    | CREDIT_BLOCK            | SO-2001 | ORDER_MANAGER, $100 over limit |
| EVT-CB-002    | CREDIT_BLOCK            | SO-2002 | FINANCE_DIRECTOR, $5 500 over  |
| EVT-MPE-001   | MASS_PRICING_ERROR      | SO-3001 | 15 lines → RED shadow → BLOCKED |
| EVT-DPO-001   | DUPLICATE_PO            | PO-9001 | composite score 0.98 → AUTO_BLOCK |
| EVT-DPO-002   | DUPLICATE_PO            | PO-9002 | composite score 0.65 → SOFT_FLAG |

---

## LLM backend options

### Option A — deterministic fallback (default, no GPU needed)

No extra configuration.  The system uses `DeterministicFallbackBackend` from
`constraints/fallback_backend.py`.

### Option B — local HuggingFace model (Outlines constrained generation)

```bash
export LOCAL_LLM_BACKEND_CLASS=tests.sandbox.llm.local_backend.LocalHFBackend
export LOCAL_LLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct   # default
export LOCAL_LLM_DEVICE=cpu                          # cpu | cuda | mps

PYTHONPATH=. streamlit run tests/sandbox/ui/app.py
```

`LocalHFBackend` uses Outlines to constrain token generation to valid JSON
matching `IntentDecision`, `RecipeProposal`, and `ShadowDecisionSchema`.
If the model fails to load (missing deps, no network) it falls back to
`DeterministicFallbackBackend` automatically.

---

## Explain / dry-run mode

```bash
ASOE_EXPLAIN_MODE=1 PYTHONPATH=. streamlit run tests/sandbox/ui/app.py
```

The pipeline runs the full classify → shadow → select_recipe path but
replaces `execute_recipe` with `explain_only`, returning
`MANUAL_REVIEW_REQUIRED` with a dry-run summary and no side effects.

---

## Architecture notes

- `seed.py` is the single source of truth for sandbox data.  Modify it to
  add new test scenarios; re-run with `--reset` to refresh.
- `local_backend.py` implements the same interface as `OutlinesConstrainedBackend`
  and is injected via `LOCAL_LLM_BACKEND_CLASS` — no production code is modified.
- The Streamlit UI calls `run_graph()` directly, exercising the full
  LangGraph state machine including Compliance Shadow and all Phase 7
  gateway hooks.
