# ASOE Sandbox

Local execution environment for testing the full Skill-Shadow-Recipe pipeline
without any cloud services.  Uses a SQLite database for seed data and
(optionally) a local open-weights model for constrained LLM generation.

Architecture_v3.md compliant: supports PostgreSQL, Redis pub/sub, JWT auth,
RBAC, and the 19 REST endpoints via the FastAPI API layer.

```
tests/sandbox/
├── conftest.py             <- shared fixtures (client, tokens, payloads)
├── test_integration.py     <- full API+Auth+DB+WS integration tests
├── test_auth_flow.py       <- multi-step login, SSO, MFA, token refresh
├── test_db_persistence.py  <- DB state verification after Recipe execution
├── test_websocket_events.py<- WebSocket/Redis pub/sub event verification
├── test_compliance_simulation.py <- shadow simulation, force BLOCKED/REVIEW
├── test_recipe_integrity.py <- recipe integrity, naming, constrained outputs
├── db/
│   └── schema.sql          <- authoritative schema reference
├── llm/
│   ├── __init__.py
│   ├── local_backend.py    <- LocalHFBackend (Outlines + HuggingFace)
│   └── prompts.py          <- human-readable prompt templates (used by UI)
├── ui/
│   ├── __init__.py
│   └── app.py              <- Streamlit trace visualiser
├── cli.py                  <- headless CLI runner (direct + API modes)
├── seed.py                 <- SQLite seeder (generates sandbox.db)
├── requirements-sandbox.txt
├── .gitignore
└── README.md               <- this file
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
# -> tests/sandbox/sandbox.db  (18 EDI events covering all 4 intents)

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

## Seeded data summary

### Reference tables

| Table                | Count | Description                                   |
|----------------------|-------|-----------------------------------------------|
| customers            |    10 | Retailer master data (name, region, tier)     |
| distribution_centers |     5 | Fulfillment locations across 4 US regions     |
| sap_pricing          |    10 | SKU master with base prices and categories    |
| retailer_contracts   |    15 | Negotiated contract prices per retailer + SKU |
| promotions           |     4 | Seasonal/clearance/bundle promotions          |
| credit_profiles      |     8 | Credit limits and current exposure per retailer |

### EDI events (18 scenarios)

| Event ID      | Intent                  | Order   | Retailer | Notes                                    |
|---------------|-------------------------|---------|----------|------------------------------------------|
| EVT-CC-001    | CONTRACTUAL_CORRECTION  | SO-1001 | R-01     | 10% discount — within 15%                |
| EVT-CC-002    | CONTRACTUAL_CORRECTION  | SO-1002 | R-02     | 8% discount — within 15%                 |
| EVT-CC-003    | CONTRACTUAL_CORRECTION  | SO-1003 | R-03     | 22% discount — exceeds threshold -> FAILED |
| EVT-CC-004    | CONTRACTUAL_CORRECTION  | SO-1004 | R-06     | 12% discount — within threshold           |
| EVT-CC-005    | CONTRACTUAL_CORRECTION  | SO-1005 | R-08     | 13% PREMIUM discount — near edge          |
| EVT-CC-006    | CONTRACTUAL_CORRECTION  | SO-1006 | R-01     | 14.7% — very close to threshold edge      |
| EVT-CC-007    | CONTRACTUAL_CORRECTION  | SO-1007 | R-03     | 20% — exceeds threshold -> FAILED         |
| EVT-CB-001    | CREDIT_BLOCK            | SO-2001 | R-01     | ORDER_MANAGER, $100 over limit            |
| EVT-CB-002    | CREDIT_BLOCK            | SO-2002 | R-05     | FINANCE_DIRECTOR, $5,500 over -> REJECTED |
| EVT-CB-003    | CREDIT_BLOCK            | SO-2003 | R-07     | ORDER_MANAGER, $4,500 over (tolerance)    |
| EVT-CB-004    | CREDIT_BLOCK            | SO-2004 | R-10     | FINANCE_DIRECTOR, $5,200 over -> REJECTED |
| EVT-MPE-001   | MASS_PRICING_ERROR      | SO-3001 | R-02     | 15 lines -> RED shadow -> BLOCKED         |
| EVT-MPE-002   | MASS_PRICING_ERROR      | SO-3002 | R-06     | 25 lines -> RED shadow -> BLOCKED         |
| EVT-MPE-003   | MASS_PRICING_ERROR      | SO-3003 | R-08     | 11 lines -> just over threshold -> BLOCKED |
| EVT-DPO-001   | DUPLICATE_PO            | PO-9001 | R-04     | composite 0.98 -> AUTO_BLOCK              |
| EVT-DPO-002   | DUPLICATE_PO            | PO-9002 | R-04     | composite 0.65 -> SOFT_FLAG               |
| EVT-DPO-003   | DUPLICATE_PO            | PO-9003 | R-09     | composite 0.82 -> REVIEW_REQUIRED         |
| EVT-DPO-004   | DUPLICATE_PO            | PO-9004 | R-06     | composite 0.40 -> CLEAR                   |

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

The pipeline runs the full classify -> shadow -> select_recipe path but
replaces `execute_recipe` with `explain_only`, returning
`MANUAL_REVIEW_REQUIRED` with a dry-run summary and no side effects.

---

## Integration test suite

Run the full sandbox integration tests:

```bash
python -m pytest tests/sandbox/ -v
```

### Test coverage

| Module | Tests | What it covers |
|--------|-------|----------------|
| `test_integration.py` | ~40 | Full API path: auth, resolve, CRUD, stats, tenant isolation |
| `test_auth_flow.py` | ~20 | Multi-step login (email → MFA), SSO, refresh, RBAC, env isolation |
| `test_db_persistence.py` | ~20 | Exception CRUD, trace storage, policy audit log, pagination |
| `test_websocket_events.py` | ~20 | WSEvent schemas, pub/sub, tenant isolation, resolve event publishing |
| `test_compliance_simulation.py` | ~20 | Force BLOCKED/REVIEW, kill switch, shadow verdicts, approve/reject |
| `test_recipe_integrity.py` | ~15 | Recipe registry, ERP mocking, constrained outputs, naming, policy |

### CLI modes

```bash
# Direct mode (default) — calls run_graph() via Python imports
PYTHONPATH=. python tests/sandbox/cli.py

# API mode — authenticates via /api/auth/login, uses REST endpoints
PYTHONPATH=. python tests/sandbox/cli.py --api

# Force BLOCKED state (RED shadow) for all events
PYTHONPATH=. python tests/sandbox/cli.py --force-blocked

# Force MANUAL_REVIEW_REQUIRED state (explain mode)
PYTHONPATH=. python tests/sandbox/cli.py --force-manual-review

# Lily personality for conversational output
PYTHONPATH=. python tests/sandbox/cli.py --api --lily
```

### Setup script

```bash
# Full setup (PostgreSQL + Redis containers)
scripts/setup-sandbox.sh

# CI mode (SQLite, no containers)
scripts/setup-sandbox.sh --ci

# Reset and recreate
scripts/setup-sandbox.sh --reset
```

---

## Architecture notes

- `seed.py` is the single source of truth for sandbox data.  Modify it to
  add new test scenarios; re-run with `--reset` to refresh.
- `local_backend.py` implements the same interface as `OutlinesConstrainedBackend`
  and is injected via `LOCAL_LLM_BACKEND_CLASS` — no production code is modified.
- The Streamlit UI calls `run_graph()` directly, exercising the full
  LangGraph state machine including Compliance Shadow and all Phase 7
  gateway hooks.
- The CLI supports both direct (`run_graph()`) and API (`/api/v1/exceptions/resolve`)
  modes, with JWT authentication via the multi-step login flow.
- Tests mock ERP (SAP/Manhattan) responses via `StubGateway`, never recipe logic.
- All machine-consumed outputs are constrained via Outlines/Guidance schemas.
