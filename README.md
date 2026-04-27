## Project Vision

ASOE (Agentic System of Engagement) is a control tower for enterprise operations where AI agents and humans collaborate to resolve exceptions across systems like SAP and Oracle ERP.

### What it does
- Diagnoses root causes of operational and financial exceptions
- Executes deterministic, auditable resolution workflows
- Communicates outcomes to customers, suppliers, and internal stakeholders

### Why it matters
In enterprise environments, automation must be:
- **Predictable** (deterministic execution)
- **Auditable** (traceable decisions)
- **Compliant** (policy-enforced actions)

ASOE ensures all three by combining AI reasoning with strict execution governance.

### Core architecture
ASOE is built on a **Skill–Shadow–Recipe** model:
- **Skills** define capabilities
- **Recipes** define deterministic workflows
- **Compliance Shadow** enforces policy before execution

### Design Principle: Deterministic Execution

In enterprise workflows, predictability is non-negotiable.

ASOE separates:
- **AI reasoning** → classification, context understanding, explanation
- **Execution** → deterministic, reproducible recipes

This ensures every action is:
- Auditable
- Repeatable
- Explainable

## System Context — The Order Pipeline and Where EMS Fits

Enterprise order-to-cash workflows are powered by an **order pipeline** — a chain of statuses and transactions an order goes through from creation to completion. This pipeline is built on three distinct layers, each with a clear responsibility:

```mermaid
graph TB
    subgraph OMS["OMS Layer — System of Action"]
        direction LR
        OMS_DESC["Captures orders from EDI 850, API, email, portal<br/>Validates, routes to warehouse, updates shipping<br/>Manages operational lifecycle: what is happening NOW<br/><i>Examples: SAP SD, Oracle OMS, NetSuite, Dynamics</i>"]
    end

    subgraph EMS["EMS Layer — Control Tower (ASOE)"]
        direction LR
        CLASSIFY["Classify<br/>Intent"] --> SHADOW["Shadow<br/>Audit"]
        SHADOW --> SELECT["Select<br/>Recipe"]
        SELECT --> EXECUTE["Execute<br/>Recipe"]
        EXECUTE --> NOTIFY["Notify<br/>Buyer"]
    end

    subgraph ERP["ERP Layer — System of Record"]
        direction LR
        ERP_DESC["Processes clean data: invoicing, general ledger,<br/>condition records, credit, procurement, payroll<br/>Manages financial lifecycle: what HAS happened<br/><i>Examples: SAP S/4HANA, Oracle EBS, NetSuite, Dynamics 365</i>"]
    end

    OMS -- "Orders flow down<br/>(exceptions detected)" --> EMS
    EMS -- "Clean data flows down<br/>(corrections applied)" --> ERP
    EMS -. "Reads context<br/>(gateway deps)" .-> OMS
    EMS -. "Writes corrections<br/>(gateway effects)" .-> ERP
```

### OMS Layer — Order Management System (System of Action)

The **OMS Layer** is specialized software that manages the full order lifecycle — from entry to fulfillment and returns — across multiple sales channels. It acts as an agile middle layer between customer-facing channels (website, POS, EDI) and the back-office ERP.

**What it does:**
- Pulls orders from Amazon, Shopify, EDI 850, portals, and physical stores into one dashboard
- Checks real-time stock levels and "available-to-promise" inventory
- Routes orders to the nearest warehouse for optimal fulfillment
- Updates customers on shipping status
- Pushes clean, validated order data to the ERP

**Also known as:** Order Management Software, Order Fulfillment System, Order Lifecycle Management, Order Orchestration Layer.

**ASOE reads from the OMS Layer** via gateway dependencies (e.g., "is this PO already fulfilled?", "what are the line items on the matched PO?"). OMS owns order data; ASOE queries it for exception resolution context.

### ERP Layer — Enterprise Resource Planning (System of Record)

The **ERP Layer** is the financial and operational backbone of the enterprise. Once orders are "clean" and ready for processing, they flow into the ERP for heavy-duty financial and accounting tasks.

**What it does:**
- Manages SAP condition records, pricing master data, and credit limits
- Generates invoices and updates the general ledger
- Tracks total inventory valuation
- Processes supplier procurement and employee payroll
- Maintains accounting integrity and compliance

**Also known as:** Back-office System, System of Record, Financial Management System, Enterprise Management System.

**ASOE writes corrections to the ERP Layer** via gateway effects (e.g., "apply condition type YK07 with adjusted price"). The gateway adapter translates this into the appropriate ERP API call (BAPI, OData, RFC).

### Key Differences Between OMS and ERP

| Dimension | OMS Layer | ERP Layer |
|---|---|---|
| **Role** | System of Action — *what is happening now* | System of Record — *what has happened* |
| **Focus** | How to fulfill the order (fast) | How to pay for it (accurate) |
| **Flexibility** | Agile, designed for changing customer needs | Rigid, focused on accuracy and compliance |
| **Data** | Real-time operational (inventory, shipping) | Financial and historical (ledger, invoices) |

**Together, OMS and ERP create the order pipeline.** The OMS handles the "messy" start (multi-channel capture, validation, routing), and the ERP handles the structured finish (invoicing, accounting, compliance). But between them, exceptions fall through the cracks.

### EMS Layer — Exception Management System (ASOE)

**This is where ASOE lives.** The EMS Layer is an independent orchestration layer — often called a "Control Tower" — that sits between OMS and ERP as the pipeline's **safety net**.

**Why it must be independent:**
- **Cross-system visibility** — Many exceptions happen *between* systems (e.g., OMS sent the order but ERP didn't receive it). An internal OMS tool can't see why the ERP failed; the EMS monitors both.
- **Conflict resolution** — If the OMS thinks an item is in stock but the ERP's warehouse record says it's gone, the EMS identifies the discrepancy and triggers a sync or re-route.
- **Unified dashboard** — Instead of logging into three systems to find why an order is stuck, the EMS pulls all "red flags" into one view.

**Exception responsibility by type:**

| Exception Type | Where Managed | Goal |
|---|---|---|
| Operational (wrong SKU, out of stock) | OMS Layer | Resolve before shipping/billing |
| Financial (invoice mismatch, credit limit, pricing discrepancy) | ERP Layer / EMS | Maintain accounting integrity |
| Cross-system (duplicate PO, integration failure, price mismatch between OMS and ERP) | **EMS Layer (ASOE)** | Keep the pipeline moving |

**ASOE handles the exceptions that neither OMS nor ERP can resolve alone:**

- **Pricing discrepancies** — PO price doesn't match SAP base price
- **Credit blocks** — order held due to credit limit breach
- **Duplicate purchase orders** — same PO arrives via multiple channels
- **Mass pricing errors** — systemic failures affecting many line items

ASOE classifies the exception, audits the proposed action through a Compliance Shadow, selects a deterministic recipe, and executes it — or routes to a human when policy requires it. Every decision is constrained, traced, and auditable.

---

## What ASOE Does (The EMS Pipeline)

When an exception event arrives from the OMS or ERP layer (e.g. PO price ≠ SAP base price, duplicate PO detected, credit hold triggered), ASOE:

1. **Classifies intent** — constrained to `CONTRACTUAL_CORRECTION`,
   `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `DUPLICATE_PO`, `PRICE_HOLD_RELEASE`,
   `EDI_MISMATCH`, `BACK_ORDER`, `OVER_MAX`, `MIN_ORDER_QTY`,
   `PALLET_CONFIG`, or `DELIVERY_DELAY` (no free-form text enters state
   transitions)
2. **Audits via Compliance Shadow** — returns `GREEN` / `YELLOW` / `RED`; halts
   on anything other than `GREEN`
3. **Selects a deterministic recipe** — constrained to registered names only
4. **Resolves context** — reads from OMS Layer (fulfillment status, PO details) and ERP Layer (pricing, credit) via gateway dependencies
5. **Executes the recipe** — immutable business logic, no autonomous reasoning
6. **Applies effects** — writes corrections to ERP and notifications to buyers via gateway adapters

No recipe runs unless intent is classified, shadow returns `GREEN`, and all
parameters are type-validated.

---

## Architecture overview

```mermaid
graph TD
    EVENT["OrderEvent"] --> INGEST["ingest"]
    INGEST --> CLASSIFY["classify"]
    CLASSIFY --> SKILL["load_skill"]
    SKILL --> CB{"validate_circuit_breaker"}

    CB -- "breach" --> FTH1["FAIL_TO_HUMAN"]
    CB -- "ok" --> SA{"shadow_audit"}

    SA -- "RED" --> BLOCKED["BLOCKED"]
    SA -- "YELLOW" --> MRR["MANUAL_REVIEW_REQUIRED"]
    SA -- "GREEN" --> SR["select_recipe"]

    SR --> VT["validate_types"]
    VT --> RD{"resolve_dependencies"}

    RD -- "gateway fail" --> FTH2["FAIL_TO_HUMAN"]
    RD -- "ok" --> ER["execute_recipe"]

    ER --> AE["apply_effects"]
    AE --> DONE["COMPLETE"]
```

**Key invariants:**
- Compliance Shadow always runs before `execute_recipe`
- Kill switch (`ASOE_KILL_SWITCH=1`) fires before any node runs
- Explain mode (`ASOE_EXPLAIN_MODE=1`) replaces `execute_recipe` with a read-only dry-run
- All machine-consumed outputs (intent, shadow verdict, recipe name) are constrained via Pydantic Literals — free-form model output never feeds state transitions

---

## Getting started

### One-command setup (recommended for novices)

The quickstart script handles everything — Python check, virtual environment,
dependency installation, database seeding, and test verification:

```bash
git clone https://github.com/kumarabhijit/asoe2.git
cd asoe2
bash scripts/quickstart.sh
```

That's it. When it finishes you'll see a "Ready" banner with all available
commands. Expected output: **1021 tests passed**.

**Other quickstart modes:**

```bash
bash scripts/quickstart.sh --test    # run tests only (skip install)
bash scripts/quickstart.sh --prod    # adds PostgreSQL + Redis containers
```

### Manual setup (step by step)

If you prefer to set up manually, or if the quickstart doesn't match your
environment:

#### Prerequisites

- **Python 3.11+** (3.11, 3.12, 3.13, or 3.14 — pinned in `.python-version`)
- **uv** (recommended) — fast Python package manager ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **Docker** (optional — only needed for `--prod` mode or `docker compose`)

No GPU, cloud keys, or optional packages required for development or testing.

#### Install (uv — recommended)

```bash
# 1. Clone the repository
git clone https://github.com/kumarabhijit/asoe2.git
cd asoe2

# 2. Install uv (if not already present)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Create virtual environment
uv venv --python 3.11

# 4. Activate the environment
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows

# 5. Install core + dev dependencies
uv pip install -e ".[dev]"

# 6. Install Streamlit for the sandbox UI
uv pip install streamlit

# 7. Seed the sandbox database
PYTHONPATH=. python tests/sandbox/seed.py
```

> **Python 3.14 users:** Run the pydantic compatibility patch after
> installing: `bash scripts/apply-patches.sh .venv/bin/python`.
> Not needed on Python 3.11–3.13.

<details>
<summary><b>Alternative: Install with pip (if uv is not available)</b></summary>

```bash
git clone https://github.com/kumarabhijit/asoe2.git
cd asoe2
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install streamlit
PYTHONPATH=. python tests/sandbox/seed.py
```

</details>

**Optional extras:**

```bash
# Outlines constrained-generation backend (GPU-heavy, not needed for CI)
uv pip install -e ".[outlines]"

# PostgreSQL driver (for production-like database testing)
uv pip install -e ".[postgres]"

# LangFuse observability (trace forwarding)
uv pip install -e ".[langfuse]"
```

#### Run the tests

```bash
python -m pytest
```

Expected: **1021 passed, 0 failed**.

| Test group | Count | What it covers |
|---|---|---|
| Core tests (`tests/test_*.py`) | 772 | Contracts, constraints, recipes, orchestration, shadow, API, DB, WebSocket, workflows, guardrails |
| E2E data-flow (`tests/test_e2e_*.py`, `tests/test_three_tier_hitl.py`) | 98 | Full pipeline for DUPLICATE_PO, CONTRACTUAL_CORRECTION, CREDIT_BLOCK, MASS_PRICING_ERROR, PRICE_HOLD_RELEASE, EDI_MISMATCH (incl. PRICE_MISMATCH classifier fork); HITL disposition + escalate + cosign |
| Sandbox integration (`tests/sandbox/test_*.py`) | 127 | Full API integration, auth flow, DB persistence, WebSocket events, compliance simulation, recipe integrity |
| PostgreSQL integration (`tests/test_postgres.py`) | 35 | Real PostgreSQL: schema migration, RLS tenant isolation, SOX immutability trigger, JSONB round-trips, UUID columns, repository CRUD, DatabaseBackedStore |

```bash
# Run only sandbox integration tests
python -m pytest tests/sandbox/ -v

# Run only PostgreSQL integration tests (requires running PostgreSQL)
ASOE_TEST_POSTGRES_URL=postgresql://asoe_test:asoe_test@localhost/asoe_test \
    python -m pytest tests/test_postgres.py -v

# Run a specific test file
python -m pytest tests/sandbox/test_auth_flow.py -v
```

#### Smoke test

```bash
python main.py
```

This runs a single demo `OrderEvent` through the full graph and prints the
resulting `GraphState`. Honoured by kill switch and explain mode.

### Sandbox CLI (headless runner)

The CLI runner executes sandbox scenarios through the full pipeline and prints
execution traces to the terminal. Supports two modes:

- **Direct mode** (default) — calls `run_graph()` via Python imports
- **API mode** (`--api`) — authenticates via `/api/auth/login` multi-step flow,
  then uses the 19 REST endpoints from architecture_v3.md

```bash
# 1. Seed the SQLite database (if not already done)
PYTHONPATH=. python tests/sandbox/seed.py

# 2. Run all 22 seeded events (direct mode)
PYTHONPATH=. python tests/sandbox/cli.py

# 3. Run via REST API endpoints (API mode with JWT auth)
PYTHONPATH=. python tests/sandbox/cli.py --api

# 4. Run a single event
PYTHONPATH=. python tests/sandbox/cli.py --event EVT-CC-001

# 5. Filter by intent
PYTHONPATH=. python tests/sandbox/cli.py --intent CREDIT_BLOCK

# 6. Force BLOCKED state (RED shadow — test guardrail UI)
PYTHONPATH=. python tests/sandbox/cli.py --force-blocked

# 7. Force MANUAL_REVIEW_REQUIRED state (explain mode)
PYTHONPATH=. python tests/sandbox/cli.py --force-manual-review

# 8. Lily personality (conversational output)
PYTHONPATH=. python tests/sandbox/cli.py --api --lily

# 9. Show full JSON trace and prompt previews
PYTHONPATH=. python tests/sandbox/cli.py --event EVT-CC-001 --json --prompts

# 10. Summary only (suppress per-event traces)
PYTHONPATH=. python tests/sandbox/cli.py --quiet
```

The runner uses `DeterministicFallbackBackend` by default. Set
`LOCAL_LLM_BACKEND_CLASS` to use a real constrained-generation model (see
environment variables below).

### Sandbox UI (local interactive visualiser)

The Streamlit UI provides an interactive browser-based sandbox with all the
same capabilities as the CLI, plus visual panels for auth flow, DB persistence,
WebSocket events, and dashboard stats.

```bash
# 1. Install sandbox dependencies (streamlit only; outlines/transformers optional)
uv pip install streamlit

# 2. Seed the SQLite database with sample SAP pricing, contracts, and EDI events
PYTHONPATH=. python tests/sandbox/seed.py

# 3. Launch the UI
PYTHONPATH=. streamlit run tests/sandbox/ui/app.py
# → open http://localhost:8501
```

**UI features (sidebar controls):**

| Control | What it does |
|---|---|
| **Execution mode** | Toggle between Direct (`run_graph()`) and API (REST endpoints with JWT) |
| **Force BLOCKED** | Override to trigger RED Compliance Shadow verdict |
| **Force MANUAL_REVIEW** | Enable explain mode (pipeline runs, no recipe execution) |
| **Lily personality** | Conversational output from the Lily agentic persona |

**Expandable panels (after running an event):**

| Panel | What it shows |
|---|---|
| Auth flow validation | Multi-step login steps, SSO init test, token refresh test |
| DB persistence | Exception record + trace record fetched back from store |
| WebSocket events | Pub/sub events published during resolve |
| Dashboard stats | Aggregate metrics from `GET /api/v1/exceptions/stats` |
| Exception queue | Paginated list from `GET /api/v1/exceptions` |
| Execution trace | Step-by-step pipeline trace (same as CLI output) |
| Prompt preview | LLM prompts for intent, recipe, shadow decisions |
| Full JSON trace | Complete `GraphState` as JSON |

**Optional: use a local open-weights model** (requires GPU or fast CPU)

```bash
pip install -r tests/sandbox/requirements-sandbox.txt
export LOCAL_LLM_BACKEND_CLASS=tests.sandbox.llm.local_backend.LocalHFBackend
export LOCAL_LLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct   # or any HF model
PYTHONPATH=. streamlit run tests/sandbox/ui/app.py
```

`LocalHFBackend` uses Outlines constrained-JSON generation — the same
guarantee as `OutlinesConstrainedBackend` in production.  If the model fails
to load it falls back silently to `DeterministicFallbackBackend`.

### API server

The FastAPI API server provides REST endpoints for exception resolution,
CRUD, workflows, policy management, and auth (architecture_v3.md §8).

```bash
# Start the API server (dev mode with auto-reload)
PYTHONPATH=. uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload

# → API docs at http://localhost:8000/docs
# → Health check: curl http://localhost:8000/api/v1/health
```

**Sandbox gateway stubs.** When `ASOE_ENV=sandbox` (default), the
server registers an in-process StubGateway set at startup
(`api/sandbox_gateways.py`) covering OMS + SAP doc / contract /
block-status + customer-master + SLA contract + promotion +
buyer-notification. This mirrors `tests/conftest.py` and lets every
recipe's `GatewayDependency` chain resolve without real ERP
connectivity. Production deployments (`ASOE_ENV=production`) skip
the stubs — the platform team is responsible for registering real
gateway adapters before serving traffic.

### End-to-end with asoe-ui

The companion frontend at
[`kumarabhijit/asoe-ui`](../asoe-ui) connects to this API server
when `NEXT_PUBLIC_USE_REAL_API=1`. A minimal local walkthrough:

```bash
# Backend (sandbox + SQLite + stub gateways auto-registered)
cd asoe2
DATABASE_URL=sqlite:///asoe2.db ASOE_ENV=sandbox JWT_SECRET=local-e2e-secret \
  PYTHONPATH=. uvicorn api.app:app --host 127.0.0.1 --port 8000

# Frontend (real-API mode pointing at the local backend)
cd ../asoe-ui
cat > .env.local <<EOF
NEXTAUTH_URL=http://localhost:3000
NEXTAUTH_SECRET=local-e2e-nextauth-secret
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_USE_REAL_API=1
EOF
npm run dev
# → open http://localhost:3000, log in as sarah.chen@acme-corp.com
#   (any password works in sandbox); seed events via curl on the
#   resolve endpoint and watch them appear in the queue.

# Playwright (auto-starts UI on :3100; reuses existing :8000 backend)
cd ../asoe-ui
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers ASOE2_ROOT=../asoe2 \
  npx playwright test
```

**Key endpoints:**

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/health` | public | Health check + dynamic enum serving |
| `POST` | `/api/v1/exceptions/resolve` | analyst+ | Synchronous exception resolution |
| `POST` | `/api/v1/exceptions/resolve/explain` | analyst+ | Explain mode dry-run |
| `GET` | `/api/v1/exceptions` | analyst+ | Paginated exception queue |
| `GET` | `/api/v1/accounts` | analyst+ | Account list (filtered by user's assigned accounts) |
| `PATCH` | `/api/v1/exceptions/{id}/disposition` | analyst+ (`exceptions:approve`); `exceptions:override` when chosen differs from recommended | Unified HITL disposition — server derives `sub_type` (APPROVE / REJECT / OVERRIDE) |
| `POST` | `/api/v1/exceptions/{id}/escalate` | analyst+ (`exceptions:escalate`) | Route an exception to ESCALATED (no resolution asserted) |
| `POST` | `/api/v1/exceptions/{id}/override/cosign` | manager+ | Four-eyes second reviewer on a high-value override (PENDING_COSIGN → RESOLVED or prior state) |
| `POST` | `/api/v1/workflows` | manager+ | Multi-step workflow execution |
| `PUT` | `/api/v1/policies/{tenant_id}` | admin | Policy override update |
| `POST` | `/api/auth/switch` | any (sandbox) | Switch to a different user (sandbox only) |
| `GET` | `/api/auth/users` | any (sandbox) | List available users (sandbox only) |

**Unified HITL disposition model (Phase 19 — Override Action consolidation):**

`PATCH /exceptions/{id}/disposition` is the single primitive for Approve,
Reject, and Override. The request body is
`{ action, notes, reason_tag }`. The server derives `sub_type` from the
chosen action vs. the agent's `recommended_action`:

- `action == "NO_ACTION"`         → `sub_type = REJECT`
- `action == recommended_action`  → `sub_type = APPROVE`
- `action != recommended_action`  → `sub_type = OVERRIDE` (requires
  `exceptions:override`, triggers Segregation of Duties and four-eyes
  checks)

A single `EXCEPTION_RESOLVED` audit event is emitted with the derived
`sub_type` in `new_value`. `reason_tag` is required and validated against
`AllowedOverrideReasonTag` (or the per-intent subset when a curated one
is published — see `constraints/specs.py` → `INTENT_REASON_TAGS`). The
legacy `/override`, `/approve`, and `/reject` endpoints and the
`EXECUTING` lifecycle state were retired in Phase 19 — the full
lifecycle is 12 states (`contracts/models.py` → `LIFECYCLE_STATES`),
including `PENDING_COSIGN` for staged high-value overrides.

Pass an optional `Idempotency-Key` header to make retries safe. Reusing
the key with a different body returns `409 IDEMPOTENCY_CONFLICT`.

High-value overrides (`financial_impact_usd >= HIGH_VALUE_OVERRIDE_THRESHOLD_USD`
in `contracts/policy.py`; default `10_000.0`) stage to `PENDING_COSIGN`
and require a second manager via `/override/cosign`. The cosigner must
differ from the initiator (SoD); a caller whose `user.sub` matches the
record's prior `resolved_by` is rejected with `403 SOD_VIOLATION` on
the disposition itself.

`/escalate` is a separate routing primitive (no resolution asserted)
with its own permission (`exceptions:escalate`, analyst+) and its own
audit event (`EXCEPTION_ESCALATED`).

**Login credentials (sandbox):**

All 6 seed users accept any non-empty password (V1 stub auth). Production will validate against a real IdP.

| Email | Name | Role | Account scope |
|---|---|---|---|
| `jane@acme.com` | Jane Doe | admin | All accounts |
| `marcus.webb@acme-corp.com` | Marcus Webb | admin | All accounts |
| `sarah.chen@acme-corp.com` | Sarah Chen | manager | All accounts |
| `sarah.chen.sr@acme-corp.com` | Sarah Chen | analyst (Sr.) | All accounts |
| `james.ortiz@acme-corp.com` | James Ortiz | analyst | Walmart, Kroger |
| `priya.nair@acme-corp.com` | Priya Nair | analyst | Target, Costco |

**Sandbox user switching:** `POST /api/auth/switch` issues a new JWT for a different user without re-authenticating. Requires a valid current JWT. Blocked in production via `ASOE_ENV` check. `GET /api/auth/users` lists all available users for the sandbox user switcher (also blocked in production).

All protected endpoints require a JWT Bearer token in the `Authorization`
header. Set `ASOE_JWT_SECRET` for production; the dev fallback is used when
unset. See `DESIGN.md` §15 for the full endpoint table and RBAC matrix.

### Audit trail (hash-chained, append-only)

`policy_audit_log` is the SOX audit record for every policy change and
every human governance action on an exception. As of Phase 20 it is a
**tamper-evident, append-only** structure:

- Each row carries `prev_hash` and
  `event_hash = sha256(prev_hash || canonical_json(row))`. The first
  row per tenant chains from `GENESIS`.
- `BEFORE UPDATE` and `BEFORE DELETE` triggers on `policy_audit_log`
  raise `policy_audit_log is append-only` — a casual `psql` session
  cannot edit or delete a row without first dropping the trigger,
  which itself leaves an obvious trail.
- Chains are per-tenant (no cross-tenant contamination).
- To verify: call
  `PolicyRepository.verify_audit_chain(tenant_id)` (or
  `exception_store.verify_audit_chain(tenant_id)` for the in-memory
  backend). Returns `(True, None)` on a clean chain, or
  `(False, first_break_idx)` pointing at the first mismatched row.

Migration: `db/migrations/V003__audit_hash_chain.sql` adds the
columns, the triggers, and backfills existing rows into a valid chain.
The runner applies the equivalent SQLite-compatible subset for CI.

### Remote LLM providers (optional, default OFF)

ASOE's constraint backend is per-task and provider-agnostic. Every
trio call (`classify_intent` / `propose_recipe` / `shadow_decision`)
can be served by Anthropic, OpenAI / Azure OpenAI / vLLM-compatible
endpoints, Ollama, HuggingFace, or the deterministic rule engine —
runtime-switchable via env vars without redeploying. Default is
`fallback` (no LLM, no egress).

**Install the provider's optional dep group:**

```bash
uv pip install "asoe[anthropic]"        # Anthropic / Foundry
uv pip install "asoe[openai]"           # OpenAI, Azure OpenAI, vLLM, TGI, LiteLLM
uv pip install "asoe[ollama]"           # Ollama (Qwen2.5+, Llama 3.1+, Mistral)
uv pip install "asoe[huggingface]"      # HF Dedicated / Serverless Inference
```

**Switch a single task to a provider** (others stay deterministic):

```bash
export ASOE_LLM_PROVIDER_INTENT=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
# Recipe and shadow stay on the deterministic backend
```

**Switch globally**:

```bash
export ASOE_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
# Optional: route via Azure AI Foundry private endpoint
# export ANTHROPIC_BASE_URL=https://my-foundry.private.example/anthropic
```

**Run Qwen on a self-hosted vLLM cluster** (OpenAI-compatible):

```bash
export ASOE_LLM_PROVIDER=openai
export OPENAI_API_KEY=any-non-empty-placeholder
export OPENAI_BASE_URL=https://my-vllm.private.example/v1
export OPENAI_MODEL=Qwen/Qwen2.5-32B-Instruct
```

**Run Qwen on local Ollama**:

```bash
export ASOE_LLM_PROVIDER=ollama
export OLLAMA_BASE_URL=http://localhost:11434
export OLLAMA_MODEL=qwen2.5
```

**Run Qwen on a HuggingFace Dedicated Inference Endpoint**:

```bash
export ASOE_LLM_PROVIDER=huggingface
export HUGGINGFACE_API_KEY=hf_...
export HUGGINGFACE_BASE_URL=https://my-endpoint.endpoints.huggingface.cloud
export HUGGINGFACE_MODEL=Qwen/Qwen2.5-32B-Instruct
```

**Mid-incident kill-by-task** (no redeploy):

```bash
# Pin shadow to deterministic only — keep intent + recipe on Anthropic
export ASOE_LLM_DISABLE_FOR=shadow

# Total LLM-tier kill — every trio call falls back to deterministic
export ASOE_LLM_DISABLE_FOR=intent,recipe,shadow

# Bigger hammer — no TCP egress at all (also short-circuits the graph)
export ASOE_KILL_SWITCH=1
```

**Cost guardrails:**

```bash
# Daily USD spend cap — hard-blocks the LLM tier and routes to
# deterministic when reached. Default is $5 (sandbox shakeout sizing).
export ASOE_LLM_DAILY_USD_BUDGET=20.00
```

The cap is Redis-backed (atomic INCRBYFLOAT); falls back to an
in-process counter when `REDIS_URL` is unset (dev only).

**Production policy gates:**
- `ASOE_ENV=production` blocks public-cloud egress for every
  provider — operators must configure a private endpoint URL
  (Azure AI Foundry, Azure OpenAI, HF Dedicated, vLLM, etc.).
- `ASOE_EXPLAIN_MODE=1` pins all tasks to deterministic so dry-runs
  never incur paid LLM calls.
- LLM-tier circuit breaker (separate from the $10k batch breaker)
  trips at error_rate > 25% / 60s OR p95_latency > 15s, with a
  5-minute cooldown.

**Provenance audit:** Every trio call recorded by `RemoteLLMBackend`
gets attached to the run's `TraceRecord` (one `LLMCallTrace` per
call) with provider / model_id / request_id / token usage / cache
hits / latency / cost / fallback reason / cross-check signals. The
LangFuse sink emits one `generation`-typed observation per call.
Audit-bearing fields documented in
`compliance/audit_bearing_registry.yaml::LLMProvenance` (pending
compliance sign-off).

See `DESIGN.md` §2 for the full provider matrix and `.env.example`
for the complete env-var inventory.

---

### LangFuse observability (optional)

Every `run_graph()` call emits a `TraceRecord` to the stdlib `asoe.observability`
logger.  When LangFuse is configured, the same record is also forwarded to
LangFuse as a trace with spans — no code changes needed.

**Install:**

```bash
# Optional — only if you want LangFuse forwarding
uv pip install "langfuse>=2.0.0"
```

**Configure (env vars or `.env`):**

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
# Omit LANGFUSE_HOST for LangFuse Cloud; set for self-hosted:
# export LANGFUSE_HOST=https://langfuse.your-domain.com
```

**What gets sent to LangFuse:**

| LangFuse entity | ASOE source |
|---|---|
| `trace.id` | `TraceRecord.trace_id` |
| `trace.name` | `"asoe-graph-execution"` |
| `trace.input` | `{ event_id }` |
| `trace.output` | `{ final_status, explanation }` |
| `trace.metadata` | `{ constrained_output_schemas, gateway_calls, rag_chunks }` |
| span `classify` | `intent_selected` |
| span `load_skill` | `skill_name` |
| span `shadow_audit` | `shadow_verdict`, `shadow_policy_hits` (level=WARNING if non-GREEN) |
| span `execute_recipe` | `recipe_name` |
| **generation** `llm.intent` / `llm.recipe` / `llm.shadow` | One per LLM call when a remote provider serves the trio. Native LangFuse generation observation: `model` = resolved model_id, `usage` = `{input, output, total, unit:"TOKENS"}`, `metadata` = provider, request_id, prompt_hash, cache hits, cost_usd_estimate, fallback flags, cross-check signals. `level=WARNING` on fallback or cross-check disagreement. **Prompt content NEVER forwarded** — only hashes. |
| score `terminal_status` | 1.0 if COMPLETE, 0.0 otherwise |

**`terminal_status` score values:** This score enables LangFuse dashboard
filtering (success vs failure), success-rate tracking over time, and alerting.
The `comment` field preserves the exact status for root-cause analysis.

| `final_status` | Score `value` | Meaning |
|---|---|---|
| `COMPLETE` | **1.0** | Recipe executed successfully |
| `FAIL_TO_HUMAN` | 0.0 | Escalated to human (circuit breaker, missing params, gateway failure) |
| `MANUAL_REVIEW_REQUIRED` | 0.0 | Shadow returned YELLOW — requires review |
| `BLOCKED` | 0.0 | Shadow returned RED — halted by policy |
| `REJECTED` | 0.0 | Rejected by policy |

**Failure isolation:** LangFuse errors are caught and logged; they never block
graph execution.  Stdlib logging remains the authoritative audit record.

**Run observability tests (including LangFuse):**

```bash
# All observability + LangFuse sink tests (no LangFuse keys needed — tests use mocks)
python -m pytest tests/test_observability.py -v

# Run just the LangFuse-specific test classes
python -m pytest tests/test_observability.py -v -k "LangFuse"
```

The LangFuse tests cover: disabled mode (no keys / no package), mock client
forwarding (trace creation, span creation per pipeline stage, shadow level,
terminal status scores, exception isolation), and `Tracer.emit()` dual-emit
(stdlib + LangFuse sink).  All tests are network-free — no live LangFuse
connection required.

**Run sandbox with LangFuse forwarding:**

```bash
# CLI runner — traces are forwarded automatically; --langfuse-flush ensures
# all pending traces are sent before the process exits
LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
  PYTHONPATH=. python tests/sandbox/cli.py --langfuse-flush

# Streamlit UI — traces are forwarded automatically on each "Run event"
LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
  PYTHONPATH=. streamlit run tests/sandbox/ui/app.py
```

Both sandbox tools show LangFuse status (enabled/disabled) in their
environment banner.  The `--langfuse-flush` flag on the CLI runner is
important for short-lived processes where the background sender may not
complete before exit.

**ASOE Docker containers:** LangFuse client is pre-installed in the `core`
and `ui` containers.  Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`
in `.env` or via `docker compose` environment — the `x-core-env` shared
block passes them to both services automatically.  In production (AKS),
keys are injected via Azure Key Vault CSI (`k8s/core/secret-provider.yaml`).

#### Setting up a LangFuse server

You need a running LangFuse server to receive traces.  There are three
options: LangFuse Cloud, Docker, or native (no Docker).

**Option A — LangFuse Cloud (no server setup)**

1. Sign up at [cloud.langfuse.com](https://cloud.langfuse.com) (free tier available)
2. Create a project → **Settings → API Keys** → generate keys
3. Export the keys:
   ```bash
   export LANGFUSE_PUBLIC_KEY=pk-lf-...
   export LANGFUSE_SECRET_KEY=sk-lf-...
   # No LANGFUSE_HOST needed — defaults to cloud.langfuse.com
   ```

**Option B — Self-hosted LangFuse via Docker**

```bash
# 1. Create a docker-compose.yml for LangFuse
cat > /tmp/langfuse-docker-compose.yml << 'EOF'
services:
  langfuse-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse
      POSTGRES_DB: langfuse
    ports:
      - "5433:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      timeout: 3s
      retries: 10

  langfuse:
    image: langfuse/langfuse:2
    depends_on:
      langfuse-db:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql://langfuse:langfuse@langfuse-db:5432/langfuse
      NEXTAUTH_SECRET: $(openssl rand -base64 32)
      NEXTAUTH_URL: http://localhost:3000
      SALT: $(openssl rand -base64 32)
      TELEMETRY_ENABLED: "false"
      LANGFUSE_INIT_ORG_ID: "asoe-org"
      LANGFUSE_INIT_ORG_NAME: "ASOE"
      LANGFUSE_INIT_PROJECT_ID: "asoe-project"
      LANGFUSE_INIT_PROJECT_NAME: "ASOE Dev"
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: "pk-lf-asoe-dev"
      LANGFUSE_INIT_PROJECT_SECRET_KEY: "sk-lf-asoe-dev"
      LANGFUSE_INIT_USER_EMAIL: "admin@asoe.local"
      LANGFUSE_INIT_USER_NAME: "ASOE Admin"
      LANGFUSE_INIT_USER_PASSWORD: "change-me-in-production"
    ports:
      - "3000:3000"
EOF

# 2. Start LangFuse
docker compose -f /tmp/langfuse-docker-compose.yml up -d

# 3. Wait for health check
curl -sf http://localhost:3000/api/public/health
# → {"status":"OK","version":"2.x.x"}

# 4. Run ASOE with the pre-provisioned keys
export LANGFUSE_PUBLIC_KEY=pk-lf-asoe-dev
export LANGFUSE_SECRET_KEY=sk-lf-asoe-dev
export LANGFUSE_HOST=http://localhost:3000
```

**Option C — Self-hosted LangFuse without Docker (native)**

Requires: Node.js (22+), PostgreSQL (16+), pnpm.

```bash
# 1. Start PostgreSQL and create database
pg_ctlcluster 16 main start
sudo -u postgres psql -c "CREATE USER langfuse WITH PASSWORD 'langfuse' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE langfuse OWNER langfuse;"

# 2. Clone LangFuse v2.95.1 (Postgres-only — no ClickHouse/S3 required)
git clone --depth 1 --branch v2.95.1 https://github.com/langfuse/langfuse.git
cd langfuse

# 3. Configure .env
cat > .env << 'ENVEOF'
DATABASE_URL=postgresql://langfuse:langfuse@localhost:5432/langfuse
DIRECT_URL=postgresql://langfuse:langfuse@localhost:5432/langfuse
NEXTAUTH_URL=http://localhost:3000
NEXTAUTH_SECRET=your-secret-here-min-32-chars-long
SALT=your-salt-here-min-32-chars-long
ENCRYPTION_KEY=0000000000000000000000000000000000000000000000000000000000000000
LANGFUSE_INIT_ORG_ID=asoe-org
LANGFUSE_INIT_ORG_NAME=ASOE
LANGFUSE_INIT_PROJECT_ID=asoe-project
LANGFUSE_INIT_PROJECT_NAME=ASOE Dev
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-asoe-dev
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-asoe-dev
LANGFUSE_INIT_USER_EMAIL=admin@asoe.local
LANGFUSE_INIT_USER_NAME=ASOE Admin
LANGFUSE_INIT_USER_PASSWORD=change-me-in-production
PORT=3000
ENVEOF

# 4. Install, migrate, build, start
npm install -g pnpm
PUPPETEER_SKIP_DOWNLOAD=1 pnpm install --no-frozen-lockfile
pnpm --filter=shared run db:migrate
pnpm run build
pnpm --filter=web run start &

# 5. Health check
curl -sf http://localhost:3000/api/public/health
# → {"status":"OK","version":"2.95.1"}

# 6. Run ASOE sandbox against the server
export LANGFUSE_PUBLIC_KEY=pk-lf-asoe-dev
export LANGFUSE_SECRET_KEY=sk-lf-asoe-dev
export LANGFUSE_HOST=http://localhost:3000
PYTHONPATH=. python tests/sandbox/cli.py --langfuse-flush

# 7. Verify traces arrived
curl -u pk-lf-asoe-dev:sk-lf-asoe-dev http://localhost:3000/api/public/traces
```

> **Note:** LangFuse v3.x+ requires ClickHouse and S3 in addition to
> PostgreSQL.  For local development, v2.95.1 is recommended as it only
> needs PostgreSQL.

### Docker (containerized — local build)

Run the full stack in containers without installing Python dependencies on
your host:

```bash
# Core orchestration + Streamlit UI (uses DeterministicFallbackBackend)
docker compose up

# → UI at http://localhost:8501

# Include local LLM inference (downloads model weights on first start)
docker compose --profile inference up
```

Copy `.env.example` to `.env` to configure kill switch, explain mode, or
model selection.  See `docker-compose.yml` for all available options.

**Behind a proxy?** Set proxy vars before building:

```bash
export HTTP_PROXY=http://proxy.example.com:8080
export HTTPS_PROXY=http://proxy.example.com:8080
docker compose build    # proxy args forwarded automatically
docker compose up
```

Proxy is a runtime option — the same images work with or without a proxy.
At runtime, set `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` in `.env` or
your shell; docker-compose passes them into containers automatically.

### Docker Hub (pre-built images — no local build required)

Pull and run the sandbox directly from Docker Hub:

```bash
# Run the sandbox UI (auto-seeds the demo database on first start)
docker compose -f docker-compose.hub.yml up

# → Sandbox UI at http://localhost:8501

# Include local LLM inference
docker compose -f docker-compose.hub.yml --profile inference up
```

** In Future: Available images on Docker Hub (`kumarabhijit/asoe`):**

| Image | Tag | Contents |
|---|---|---|
| `kumarabhijit/asoe` | `core-latest` | Core orchestration engine (LangGraph + recipes + Compliance Shadow) |
| `kumarabhijit/asoe` | `sandbox-ui-latest` | Streamlit sandbox UI (auto-seeds demo DB, no GPU needed) |
| `kumarabhijit/asoe` | `inference-latest` | Local LLM inference (Outlines + torch + transformers) |

**Pin a specific version:**

```bash
ASOE_TAG=v0.3.2 docker compose -f docker-compose.hub.yml up
```

**Behind a proxy (runtime):**

```bash
HTTP_PROXY=http://proxy:8080 HTTPS_PROXY=http://proxy:8080 \
  docker compose -f docker-compose.hub.yml up
```

**Test the sandbox step-by-step:**

1. `docker compose -f docker-compose.hub.yml up` — starts core + sandbox UI
2. Open `http://localhost:8501` in your browser
3. Select an EDI event from the sidebar (22 sample events covering all 4 intents)
4. Click **Run** — the event runs through the full pipeline: classify → shadow → recipe → effects
5. Inspect the execution trace: intent, shadow verdict, recipe, gateway activity, full JSON state
6. Toggle `ASOE_EXPLAIN_MODE=1` in `.env` for dry-run mode (no recipe side effects)

### Build and push to Docker Hub

```bash
# Login to Docker Hub
docker login

# Build and push all images (tag: latest)
bash scripts/docker-build-push.sh

# Build and push with a specific version tag
bash scripts/docker-build-push.sh v0.3.2

# Build behind a proxy
HTTP_PROXY=http://proxy:8080 bash scripts/docker-build-push.sh v0.3.2
```

### AKS production deployment

For AKS production deployment, apply the Kubernetes manifests:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/core/
kubectl apply -f k8s/ui/
kubectl apply -f k8s/inference/
```

See `architecture_v3.md` §4 for the full Azure infrastructure stack.

---

## Directory structure

```
contracts/          Typed Pydantic models — OrderEvent, GraphState, ExecutionLog, …
  policy.py         Centralised business thresholds (discount limits, circuit breaker bounds, autonomy levels, etc.)
skills/             SKILL.md files (loaded verbatim, never rewritten)
compliance/         Compliance Shadow — audit() + enforce()
constraints/        Constrained-generation schemas, backends, router
  specs.py          AllowedIntent / AllowedShadowStatus / AllowedRecipeName / AllowedResolutionAction Literals
  fallback_backend.py  DeterministicFallbackBackend (rule-based, no LLM)
  guidance_backend.py  GuidanceRegexBackend (regex patterns for Guidance / Outlines)
  router.py         get_constrained_backend() — env-driven backend selection
recipes/            Immutable business logic
  registry.py       REGISTRY — maps AllowedRecipeName → recipe spec
  executor.py       RecipeExecutor — sole entry point for recipe execution
gateways/           Infrastructure gateway layer (Hexagonal Architecture)
  base.py           InfrastructureGateway protocol (Port)
  registry.py       Gateway registry (register / lookup)
  executor.py       GatewayExecutor — tracing + error handling
  stub.py           StubGateway test double
workflows/          Multi-step workflow runner (Saga pattern)
  runner.py         WorkflowRunner — sequential steps with compensation
orchestration/      LangGraph state machine
  graph.py          build_graph() / build_explain_graph() / run_graph()
  nodes.py          One function per LangGraph node (incl. resolve_dependencies, apply_effects)
  utils.py          circuit_breaker(), compute_discrepancy()
observability/      Structured tracing with optional LangFuse forwarding
  tracer.py         TraceRecord builder + stdlib JSON emitter + LangFuse sink call
  langfuse_sink.py  Optional LangFuse forwarder (lazy init, failure-isolated, no-op without keys)
hardening/          Kill switch + explain mode implementation
api/                FastAPI API layer (architecture_v3.md §8, §11)
  app.py            Application factory (create_app())
  deps.py           JWT auth, RBAC (5 roles), tenant extraction, env isolation
  middleware.py     X-Trace-ID propagation middleware
  errors.py         Standard error envelope
  schemas.py        Request/Response Pydantic models
  store.py          Exception store (in-memory or database-backed via DATABASE_URL)
  routes/           health, exceptions, workflows, policies, auth endpoints
db/                 Database layer (architecture_v3.md §9)
  connection.py     SQLiteAdapter / PostgresAdapter + _QmarkCursorWrapper (?→%s), create_adapter() factory
  repository.py     ExceptionRepository, TraceRepository, PolicyRepository
  migrations/       V001__initial_schema.sql (5 tables, RLS, SOX trigger, pgvector); V002__reanalyze_columns.sql (original_event + reanalysis_history); V003__audit_hash_chain.sql (prev_hash/event_hash + append-only triggers); V004__enrichment_context.sql (Pillar 1 audit-evidence column)
docs/               AUDITOR_GUIDE.md, ADR-021, ADR-022, ADR-023, ADR-024, ADR-025
  specs/            Product-owner reference specs (not runtime code)
tests/              pytest test suite (1021 tests)
  test_*.py         Core tests: contracts, constraints, recipes, orchestration, shadow, API, DB, WebSocket, workflows, guardrails (772 tests)
  test_postgres.py  PostgreSQL integration tests: schema migration, RLS, SOX trigger, JSONB, UUID, repository CRUD (35 tests; requires ASOE_TEST_POSTGRES_URL)
  sandbox/          Local execution sandbox + integration tests (127 tests)
    conftest.py     Shared fixtures (client, JWT tokens for all RBAC roles, event payloads)
    test_integration.py       Full API path: health, resolve, CRUD, stats, tenant isolation (31 tests)
    test_auth_flow.py         Multi-step login, SSO, MFA, token refresh, RBAC (20 tests)
    test_db_persistence.py    Exception/trace/policy persistence, lifecycle mapping (16 tests)
    test_websocket_events.py  Pub/sub events, tenant isolation, WebSocket auth (17 tests)
    test_compliance_simulation.py  Force BLOCKED/REVIEW, kill switch, approve/reject (19 tests)
    test_recipe_integrity.py  Recipe registry, ERP mocking, naming, constraints (24 tests)
    cli.py          Headless CLI runner — direct + API modes, simulation flags, Lily personality
    seed.py         SQLite seeder — creates sandbox.db with customers, DCs, promotions, SAP / EDI data
    ui/app.py       Streamlit execution-trace visualiser — API mode, auth panel, DB verification, WebSocket monitor
    llm/local_backend.py  LocalHFBackend — Outlines + HuggingFace model (optional)
    llm/prompts.py  Human-readable prompt templates for the UI "Prompt Preview" panel
    requirements-sandbox.txt  Sandbox-only deps (streamlit, outlines, transformers, torch)
scripts/
  quickstart.sh     One-command dev setup (venv, deps, seed, tests) — start here
  setup-sandbox.sh  PostgreSQL + Redis containers, DB migrations, --ci mode for SQLite
  sandbox-entrypoint.sh  Docker entrypoint — auto-seeds sandbox.db
  apply-patches.sh  Pydantic compatibility patch for Python 3.14+
  docker-build-push.sh   Build and push Docker images to registry
Dockerfile.core     Core orchestration container (LangGraph + recipes + shadow)
Dockerfile.ui       Streamlit sandbox UI container (core + streamlit)
Dockerfile.inference  Local LLM inference container (Outlines + torch + transformers)
docker-compose.yml      Local dev stack — core + ui + postgres + redis + optional inference profile (local build)
docker-compose.hub.yml  Pull-and-run from Docker Hub (no local build required)
.dockerignore           Excludes .git, __pycache__, sandbox.db, k8s/ from images
.env.example            Documents all runtime env vars for Docker (incl. proxy)
k8s/                Kubernetes manifests for AKS production deployment
  namespace.yaml    asoe namespace with compliance label
  core/             Deployment (2 replicas), Service, ConfigMap, SecretProviderClass (Azure Key Vault CSI)
  ui/               Deployment (2 replicas), Service
  inference/        Deployment (1 replica, Intel AMX nodeSelector), Service
```

---

## Key reference documents

| Document | Read this to understand… |
|---|---|
| `CLAUDE.md` | Architecture rules, what must never change, constrained-generation policy |
| `tasks.md` | Phase-by-phase implementation checklist and acceptance criteria |
| `architecture_v3.md` | Architecture patterns and principles: Skill–Recipe decoupling, Hexagonal Gateways, Saga workflows, execution invariants |
| `DESIGN.md` | Implementation reference: module map, class/function names, graph node wiring, env vars, container layout |
| `docs/AUDITOR_GUIDE.md` | Audit controls: constrained-generation boundaries, kill switch, explain mode, 10 execution invariants |
| `contracts/policy.py` | Centralised business thresholds — discount limits, circuit breaker bounds, credit exposure tolerance |
| `docs/adr/ADR-021-core-deployment-model.md` | Library vs. service deployment decision, staged evolution triggers |
| `docs/adr/ADR-022-database-access-pattern.md` | Raw SQL vs. ORM decision, migration triggers, expert perspectives |
| `docs/adr/ADR-023-disposition-and-hash-chained-audit.md` | Unified `/disposition` primitive + hash-chained append-only audit log (Phases 1–4 of the Override Action overhaul) |
| `prompts/po-spec-to-asoe.md` | Step-by-step prompt for converting a Product Owner specification into ASOE Skill–Shadow–Recipe components |
| `prompts/triple_check_review_board.md` | Reusable review prompt — three-persona architecture, security, and test coverage assessment |
| `prompts/phase_10_langfuse.md` | LangFuse integration prompt — sink design, trace mapping, self-hosted setup, SDK compatibility, test plan |
| `prompts/phase_12_api_layer.md` | FastAPI API layer prompt — 19 endpoints, auth, RBAC, tenant isolation, error envelope |
| `prompts/phase_13_database_layer.md` | Database layer prompt — PostgreSQL schema, migrations, repository, RLS, SOX audit |
| `prompts/phase_14_auth_security.md` | Auth & security hardening prompt — token expiry, env isolation, trace_id, partner scoping |
| `prompts/phase_15_websocket_redis.md` | WebSocket/Redis prompt — event schemas, pub/sub, WebSocket hub, resolve wiring |
| `prompts/phase_16_v1_guardrails.md` | V1 Foundation Guardrail tests — 6 CI-automated guardrails (AST inspection, metadata contracts, ERP-agnostic gateway, schema agnostic) |
| `tests/sandbox/seed.py` | Sandbox seeder: customers, DCs, promotions, SAP pricing, retailer contracts, credit profiles, and 22 EDI events covering all four intents |
| `tests/sandbox/cli.py` | Headless CLI runner — direct + API modes, simulation flags, Lily personality |
| `tests/sandbox/ui/app.py` | Streamlit UI — API mode, auth panel, DB verification, WebSocket monitor, stats |
| `tests/sandbox/conftest.py` | Shared test fixtures — JWT tokens for all 5 RBAC roles, event payloads |
| `scripts/quickstart.sh` | One-command setup — venv, deps, seed, tests. Start here as a novice |
| `scripts/setup-sandbox.sh` | PostgreSQL + Redis container setup, DB migrations, `--ci` mode for SQLite |

**Start here if you are:**
- **A novice** — run `bash scripts/quickstart.sh` first, then read this README
- **A new engineer** — read `CLAUDE.md` first, then `tasks.md`, then this README
- **An auditor** — go directly to `docs/AUDITOR_GUIDE.md`
- **Adding a new recipe** — see the [Adding a new recipe](#adding-a-new-recipe) section below

---

## Phase structure

| Phase | What was built |
|---|---|
| 0 | Repo structure, typed contracts (`contracts/models.py`), constrained schemas (`constraints/specs.py`) |
| 1 | Skill loading (`skills/loader.py`, `skills/*.md`) + non-executing intent classification |
| 2 | Compliance Shadow (`compliance/shadow.py`) — GREEN / YELLOW / RED, TraceID, reasons, policy hits |
| 3 | Recipe registry + deterministic executor (`recipes/`) + immutable `ExecutionLog` |
| 4 | LangGraph state machine (`orchestration/`) + circuit breaker (>50 updates or >$10 k variance) |
| 5 | Structured tracing (`observability/tracer.py`) with optional LangFuse forwarding (`observability/langfuse_sink.py`) + golden regression tests |
| 6 | Kill switch, read-only explain mode, auditor docs, constrained-generation safeguard documentation |
| 7 | Infrastructure gateways (Ports & Adapters), multi-step workflows (Saga pattern), DUPLICATE_PO fallback routing |
| 8 | Local execution sandbox — SQLite seeder, Streamlit UI, LocalHFBackend (Outlines + HuggingFace) |
| 9 | Containerized deployment — 3 Dockerfiles (core/ui/inference), docker-compose for local dev, K8s manifests for AKS |
| Review | Triple-Check Technical Review Board — resolved 10 findings (1 Critical, 1 High, 8 Medium); 7 Low findings debated and accepted (SKIP); test count 490 → 525 → 540 (LangFuse) |
| 11 | Duplicate PO product spec gap closure — 6 resolution actions, decision tree, autonomy levels (L1–L4), override audit fields, buyer notification gateway effects; test count 540 → 584 |
| 12 | FastAPI API layer — 19 REST endpoints, JWT auth, RBAC (5 roles), tenant isolation, standard error envelope; test count 584 → 659 |
| 13 | Database layer — PostgreSQL schema (5 tables, RLS, SOX trigger), migration runner, connection adapters, repository layer, docker-compose with PostgreSQL + Redis; test count 659 → 690 |
| 14 | Auth & security hardening — token expiry (15min/7d), env isolation, X-Trace-ID propagation, partner-role scoping, configurable JWT secret; test count 690 → 718 |
| 15 | WebSocket / Redis real-time event publishing — event schemas, pub/sub manager (in-memory + Redis), WebSocket hub with JWT auth + tenant scoping, resolve endpoint event wiring; test count 718 → 739 |
| 16 | V1 Foundation Guardrail tests — 6 CI-automated guardrails (AST inspection, dynamic enums, metadata contracts, ERP-agnostic gateway, schema agnostic, policy key format) + Invariant #11; test count 739 → 764 |
| 17 | Sandbox integration tests — REST API mode CLI, multi-step auth flow tests, DB persistence verification, WebSocket event tests, compliance simulation (force BLOCKED/REVIEW), recipe integrity + naming checks, setup-sandbox.sh with PostgreSQL/Redis containers; test count 764 → 891 |
| 13.8 | PostgreSQL integration tests — 35 tests on real PostgreSQL: _QmarkCursorWrapper (?→%s), V001 schema migration (pgcrypto, pgvector, UUID, JSONB, TIMESTAMPTZ), RLS tenant isolation, SOX immutability trigger, repository CRUD, DatabaseBackedStore; UNIQUE INDEX on `exceptions.trace_id`; test count 972 → 1007 |
| 18 | Server-side user profiles & Account entity — user store (6 seed users), Account entity (4 accounts), updated auth endpoints (login, switch, users list), account scoping, JWT claims (title, avatar_initials, assigned_accounts), sandbox user switching; test count 1007 → 1021 |

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ASOE_KILL_SWITCH` | `0` | `1` / `true` / `yes` — halt all automated execution before any node runs |
| `ASOE_EXPLAIN_MODE` | `0` | `1` / `true` / `yes` — dry-run only; shadow audits but no recipe executes |
| `ASOE_ENV` | `sandbox` | `sandbox` or `production` — JWT `env` claim must match (§11.6) |
| `ASOE_JWT_SECRET` | _(dev fallback)_ | JWT signing secret — **required for production** (Key Vault-managed) |
| `DATABASE_URL` | _(unset)_ | PostgreSQL connection string; when set, API uses database-backed store |
| `ASOE_TEST_POSTGRES_URL` | _(unset)_ | PostgreSQL connection string for integration tests; when unset, PostgreSQL tests are skipped |
| `REDIS_URL` | _(unset)_ | Redis connection string for pub/sub, task queue, cache |
| `USE_OUTLINES_BACKEND` | `0` | `1` — use `OutlinesConstrainedBackend` (requires `pip install -e ".[outlines]"`) |
| `SANDBOX_DB_PATH` | `tests/sandbox/sandbox.db` | Path to the sandbox SQLite database |
| `LOCAL_LLM_BACKEND_CLASS` | _(unset)_ | Fully-qualified class to use as the constrained backend (e.g. `tests.sandbox.llm.local_backend.LocalHFBackend`) |
| `LOCAL_LLM_MODEL` | `Qwen/Qwen2.5-0.5B-Instruct` | HuggingFace model id for `LocalHFBackend` |
| `LOCAL_LLM_DEVICE` | `cpu` | Compute device for `LocalHFBackend` (`cpu` / `cuda` / `mps`) |
| `LANGFUSE_PUBLIC_KEY` | _(unset)_ | LangFuse public key — enables trace forwarding when set (requires `langfuse` package) |
| `LANGFUSE_SECRET_KEY` | _(unset)_ | LangFuse secret key — required alongside public key |
| `LANGFUSE_HOST` | _(unset)_ | LangFuse host URL — omit for LangFuse Cloud, set for self-hosted |
| `HTTP_PROXY` | _(unset)_ | HTTP proxy URL — used at build time (Dockerfile ARG) and runtime (container env) |
| `HTTPS_PROXY` | _(unset)_ | HTTPS proxy URL — same as above |
| `NO_PROXY` | _(unset)_ | Comma-separated list of hosts to bypass proxy |

No process restart required; each `run_graph()` call reads the env vars fresh.

---

## Deployment

### Azure Container Apps (pre-prod)

The FastAPI service can be deployed to Azure Container Apps with one
command after `az login`. The end-to-end runbook lives in
[`docs/deploy-azure-container-apps.md`](docs/deploy-azure-container-apps.md).

**Quick path** (one command for first deploy and every subsequent re-deploy):

```bash
az login
az account set --subscription f6f24d74-9f1a-4717-94d2-4eef4a617aa0

PG_ADMIN_PASSWORD='<strong-pw>' \
ANTHROPIC_API_KEY='sk-ant-<your-key>' \
    ./scripts/deploy-azure.sh
```

`deploy-azure.sh` provisions the infra, builds the image, derives
`DATABASE_URL` / `REDIS_URL` from infra outputs, and creates the Container
App with all four secrets populated. On re-runs it preserves
`ANTHROPIC_API_KEY` and `ASOE_JWT_SECRET` from the running app, so secrets
never regress to placeholders. Pass new values for either of those env
vars to rotate them; pass `ASOE_JWT_SECRET=auto` to force-generate a new
JWT secret.

For day-to-day rotation of just the Anthropic key (no infra change):

```bash
ANTHROPIC_API_KEY='sk-ant-NEW' ./scripts/set-secrets.sh
```

Artifacts:

- [`Dockerfile.api`](Dockerfile.api) — production FastAPI image (uvicorn,
  Python 3.14-slim, non-root, healthcheck on `/api/v1/health`).
- [`infra/main.bicep`](infra/main.bicep) — IaC for ACR + Postgres Flexible
  Server (`pgcrypto`+`vector` allow-listed) + Azure Managed Redis +
  Container Apps Environment + Container App backed by a User-Assigned
  Managed Identity with AcrPull RBAC.
- [`infra/parameters.sandbox.json`](infra/parameters.sandbox.json) —
  parameters for the `asoepreprod` environment in `centralus`.
- [`scripts/deploy-azure.sh`](scripts/deploy-azure.sh) — provisions infra,
  builds the image in ACR, points the Container App revision at the new
  image.
- [`scripts/set-secrets.sh`](scripts/set-secrets.sh) — rotation helper
  for `ANTHROPIC_API_KEY` / `ASOE_JWT_SECRET` (no infra changes).

Resource sizing (sandbox):

| Resource | SKU | Notes |
| --- | --- | --- |
| Container App | 0.5 vCPU / 1.0 GiB, min=1 / max=2 | HTTP scale rule, sticky sessions enabled for `/api/v1/ws` WebSocket |
| Postgres Flexible | `Standard_B1ms` Burstable, 32 GB | Public + `AllowAllAzureServices` firewall rule (replace with private endpoint for prod) |
| Azure Managed Redis | `Balanced_B0` (~250 MB, EnterpriseCluster) | TLS only (`rediss://…:10000`); replaces retiring Azure Cache for Redis |
| ACR | Basic | User-Assigned Managed Identity with pre-granted AcrPull |
| Log Analytics | PerGB2018, 30-day retention | Container App stdout/stderr |

CORS is env-driven: pass any combination of `corsAllowedOrigin`
(legacy single), `corsAllowedOriginsCsv` (multi-origin), or
`corsAllowedOriginRegex` (Vercel preview URLs) in
`infra/parameters.sandbox.json` and re-run the deploy script. The
Azure-hosted UI's FQDN is added to the allowlist automatically by the
bicep template (computed from `cae.properties.defaultDomain`); no
parameter change needed when you flip `DEPLOY_UI=1`.

#### Deploying asoe-ui to Azure Container Apps (pre-prod)

Vercel stays as dev / per-PR previews; pre-prod can run on Azure
alongside the API for a single audit boundary and unified
observability. Run the deploy with `DEPLOY_UI=1`:

```bash
DEPLOY_UI=1 ASOE_UI_PATH=../asoe-ui \
PG_ADMIN_PASSWORD='<strong-pw>' \
    ./scripts/deploy-azure.sh
```

This builds the asoe-ui Next.js standalone image into ACR with
`NEXT_PUBLIC_API_URL=https://<API_FQDN>` baked in, then provisions
a sister Container App (`asoepreprodui`) in the same managed
environment. `NEXTAUTH_SECRET` auto-generates on first deploy and
preserves on re-runs (pass `NEXTAUTH_SECRET=auto` to rotate).

For UI-only redeploys after an asoe-ui code change (no API/infra
touched, ~2 min):

```bash
./scripts/redeploy-ui.sh
```

#### Day-to-day Azure operations

After `az login` and `az account set --subscription
f6f24d74-9f1a-4717-94d2-4eef4a617aa0`, set these in your shell once:

```bash
RG=asoepreprod
APP=asoepreprodapi
FQDN=$(az containerapp show -g $RG -n $APP \
       --query properties.configuration.ingress.fqdn -o tsv)
```

| Task | Command |
|---|---|
| **Launch API only (first deploy)** | `PG_ADMIN_PASSWORD='<pw>' ANTHROPIC_API_KEY='sk-ant-...' ./scripts/deploy-azure.sh` |
| **Launch API + UI together** | `DEPLOY_UI=1 ASOE_UI_PATH=../asoe-ui PG_ADMIN_PASSWORD='<pw>' ANTHROPIC_API_KEY='sk-ant-...' ./scripts/deploy-azure.sh` |
| **Re-deploy API after a code change** | `PG_ADMIN_PASSWORD='<same-pw>' ./scripts/deploy-azure.sh` (secrets preserved) |
| **Re-deploy UI after a UI change** | `./scripts/redeploy-ui.sh` (~2 min, API untouched) |
| **Health check** | `curl -fsS --max-time 30 "https://${FQDN}/api/v1/health" \| jq .` |
| **Active revision status** | `az containerapp revision list -g $RG -n $APP --query "[?properties.active]" -o table` |
| **Rotate Anthropic key only** | `ANTHROPIC_API_KEY='sk-ant-NEW' ./scripts/set-secrets.sh` |
| **Rotate JWT secret** (invalidates issued tokens) | `ASOE_JWT_SECRET=auto ./scripts/set-secrets.sh` |
| **Rotate Postgres admin password** | `az postgres flexible-server update -g $RG -n asoepreprodpg --admin-password '<new>'` then re-run `deploy-azure.sh` with the new password |
| **Rotate Redis primary key** | `az redisenterprise database regenerate-key --cluster-name asoepreprodredis -g $RG -n default --key-type Primary` then re-run `deploy-azure.sh` |
| **Recent console logs** (no streaming) | see runbook's [Azure CLI cheat sheet](docs/deploy-azure-container-apps.md#azure-cli-cheat-sheet) |
| **Roll back to a previous revision** | `az containerapp ingress traffic set -g $RG -n $APP --revision-weight <older>=100` |
| **Tear down** | `az group delete -n $RG --yes --no-wait` |

Full runbook with Postgres-direct-connect, scaling, custom-domain hookup,
and a comprehensive Troubleshooting section (covering the issues we
actually hit during first deploys: `Operation expired`, ACR 401,
`pgcrypto not allow-listed`, password-with-`@`, etc.) lives in
[`docs/deploy-azure-container-apps.md`](docs/deploy-azure-container-apps.md).

---

## Engineer Cookbook

The following sections are step-by-step guides for the four most common
development tasks.  Each section references the exact files you must touch and
explains why.

---

### Adding a new SKILL

A SKILL is a markdown file that classifies intent and guides reasoning for a
specific domain.  It is loaded verbatim by `SkillLoader` — the text is injected
into the LLM context without summarisation or rewriting.

**Rule:** SKILL files contain reasoning guidance only.  Business logic lives
exclusively in recipes.

#### Step 1 — Create the SKILL file

Create `skills/<domain>_SKILL.md`.  The file must have a YAML front-matter
block followed by markdown content.  Follow the pattern from the existing skill:

```
---
name: <short-slug>
description: <one-line description>
metadata:
  version: 1.0.0
  author: <your-team>
  required_tools: [<mcp-tool-1>, ...]
  recipes: [<RecipeName>.py, ...]
  constrained_generation: [Guidance, Outlines]
---
# Skill: <Human Title>

## 1. Overview
<When is this skill triggered?>

## 2. Reasoning Loop
1. <step 1 — identify the problem>
2. <step 2 — query context>
3. <step 3 — classify intent>
4. <step 4 — select exact recipe; do not improvise logic>

## 3. Constrained Generation Policy
- intent labels must be constrained to the allowed intent enum
- recipe names must be constrained to registered recipes only
- shadow verdicts must be constrained to GREEN / YELLOW / RED

## 4. Recipe-to-Intent Mapping
- <IntentValue> -> `<RecipeName>.py`
- <IntentValue> -> FAIL_TO_HUMAN

## 5. Execution Protocol
Before calling any recipe, check the Compliance Shadow.
If the shadow returns RED, halt and explain the compliance breach.

## 6. Output Requirements
Always include the Recipe Execution Log snippet for audit traceability.
```

#### Step 2 — Register the event-type trigger

Open `skills/loader.py` and extend `select_for_event()` to return your new
skill for the relevant event types:

```python
# skills/loader.py  — select_for_event()
if "YOUR_EVENT_TYPE" in event_type.upper():
    return self.load_by_name("your-domain_SKILL.md")
```

The loader discovers `*.md` files automatically; routing still requires an
explicit branch in `select_for_event()` so selection is deterministic and
testable.

#### Step 3 — Write a test

Add a test in `tests/test_skill_loader.py`:

```python
def test_select_for_new_event_type():
    loader = SkillLoader()
    skill = loader.select_for_event("YOUR_EVENT_TYPE")
    assert skill.name == "<short-slug>"
```

#### What NOT to do

- Do not add business thresholds, dollar limits, or authorization logic to a
  SKILL file.  Those belong in a recipe.
- Do not summarise or rewrite SKILL text when loading — use `load_by_name()`
  verbatim, as `SkillLoader._parse()` already does.

---

### Defining and enforcing constraints

Constraints protect every value that flows into a state transition or execution
decision.  Free-form model output is permitted only for human-facing
`explanation` fields.  Everything else must be constrained at generation time.

There are three places that must stay in sync when you add or change a
constrained vocabulary:

| File | What it does |
|---|---|
| `constraints/specs.py` | Defines `AllowedIntent`, `AllowedShadowStatus`, `AllowedRecipeName`, `AllowedResolutionAction` Pydantic Literals and the output schemas (`IntentDecision`, `ShadowDecisionSchema`, `RecipeProposal`) |
| `constraints/fallback_backend.py` | Rule-based backend used in CI/tests; must return values in the allowed vocabulary |
| `constraints/guidance_backend.py` | Regex patterns for Guidance / Outlines backends; must match the allowed vocabulary exactly |

#### Adding a new intent value

1. **`constraints/specs.py`** — add the new string to `AllowedIntent`:

   ```python
   AllowedIntent = Literal[
       "CONTRACTUAL_CORRECTION",
       "CREDIT_BLOCK",
       "MASS_PRICING_ERROR",
       "DUPLICATE_PO",
       "PRICE_HOLD_RELEASE",
       "EDI_MISMATCH",
       "YOUR_NEW_INTENT",   # ← add here
   ]
   ```

2. **`contracts/models.py`** — add the matching `Intent` enum member:

   ```python
   class Intent(str, Enum):
       CONTRACTUAL_CORRECTION = "CONTRACTUAL_CORRECTION"
       CREDIT_BLOCK           = "CREDIT_BLOCK"
       MASS_PRICING_ERROR     = "MASS_PRICING_ERROR"
       DUPLICATE_PO           = "DUPLICATE_PO"
       PRICE_HOLD_RELEASE     = "PRICE_HOLD_RELEASE"
       EDI_MISMATCH           = "EDI_MISMATCH"
       YOUR_NEW_INTENT        = "YOUR_NEW_INTENT"   # ← add here
       UNKNOWN                = "UNKNOWN"
   ```

3. **`constraints/guidance_backend.py`** — extend the intent regex:

   ```python
   def intent_regex(self) -> str:
       return (
           r"CONTRACTUAL_CORRECTION|CREDIT_BLOCK|MASS_PRICING_ERROR|"
           r"DUPLICATE_PO|PRICE_HOLD_RELEASE|EDI_MISMATCH|YOUR_NEW_INTENT"
       )
   ```

4. **`constraints/fallback_backend.py`** — add a classification branch in
   `classify_intent()` and a recipe mapping in `propose_recipe()`.

5. **`orchestration/nodes.py`** — ensure `classify()` handles the new intent.

6. **Run `python -m pytest`** — the vocabulary sync tests in
   `tests/test_constraints.py` will catch any mismatch between `AllowedIntent`,
   the `Intent` enum, and the regex pattern.

#### Enforcing a new output schema

If you need to constrain a new LLM-produced value (e.g. a confidence band or
escalation category):

1. Define the schema in `constraints/specs.py` as a new `BaseModel` with a
   `Literal`-typed field.
2. Return the schema from the appropriate backend method.
3. Validate with Pydantic before allowing the value into `GraphState`.
4. Add the schema name to `ExecutionLog.constrained_outputs` so it appears in
   every `TraceRecord`.

Never allow a free-form string into a state transition field without wrapping it
in a constrained schema first.

---

### Extending compliance shadow logic

The Compliance Shadow (`compliance/shadow.py`) audits every proposed action
before execution.  It returns a `ComplianceDecision` with status
`GREEN | YELLOW | RED`.  Only `GREEN` allows automatic recipe execution.

**Rule:** The shadow must never select or execute a recipe.  Its sole job is
routing.

#### Understanding the two-method contract

```
ComplianceShadow.audit(state)   → ComplianceDecision   # verdict
ComplianceShadow.enforce(decision) → ShadowEnforcement # routing action
```

`audit()` calls the constrained backend.  `enforce()` translates the verdict
into `PROCEED | ESCALATE | BLOCK`.  Both are called by `orchestration/nodes.py`
→ `shadow_audit()`.

#### Adding a new policy check

All policy logic lives in `constraints/fallback_backend.py` →
`shadow_decision()`.  Do not put policy logic in `shadow.py` itself — that file
is infrastructure, not policy.

1. Open `constraints/fallback_backend.py` and add a branch in
   `shadow_decision()`:

   ```python
   def shadow_decision(self, state: GraphState) -> ShadowDecisionSchema:
       # existing checks …

       if <your_new_policy_condition>:
           return ShadowDecisionSchema(
               status="RED",   # or "YELLOW"
               reasons=["<Human-readable explanation of the policy hit>"],
               policy_hits=["YOUR_POLICY_ID"],
           )

       # default GREEN …
   ```

2. Choose the correct verdict:
   - `RED` — halt immediately; do not execute; require human review
   - `YELLOW` — route to `MANUAL_REVIEW_REQUIRED`; no automatic execution
   - `GREEN` — allow auto-proceed (reserve for confirmed safe paths)

3. Write a test in `tests/test_shadow.py`:

   ```python
   def test_new_policy_blocks_execution(make_state):
       state = make_state(<condition that triggers your policy>)
       shadow = ComplianceShadow()
       decision = shadow.audit(state)
       assert decision.status == ShadowStatus.RED
       assert "YOUR_POLICY_ID" in decision.policy_hits
   ```

4. Verify the enforcement path in `tests/test_shadow.py`:

   ```python
   def test_red_verdict_produces_block():
       decision = ComplianceDecision(
           status=ShadowStatus.RED,
           reasons=["test"],
           policy_hits=["YOUR_POLICY_ID"],
       )
       enforcement = ComplianceShadow().enforce(decision)
       assert enforcement.action == "BLOCK"
   ```

#### Using the Outlines backend in production

The `DeterministicFallbackBackend` is used in CI.  In production, set
`USE_OUTLINES_BACKEND=1` to use `OutlinesConstrainedBackend`, which constrains
the shadow verdict via a regex at LLM generation time.  The verdict regex is
defined in `constraints/guidance_backend.py` → `shadow_verdict_regex()`.  Any
new allowed verdict value must be added there as well.

---

### Introducing or modifying contracts

Contracts are Pydantic models in `contracts/models.py`.  They define the shape
of every value that crosses a module boundary.  `GraphState` in particular is
the shared state threaded through every LangGraph node.

**Rule:** `GraphState` uses `extra = "forbid"` — no untyped fields may enter
the state machine.  Every new field must be explicitly declared.

#### Adding a new field to GraphState

1. **`contracts/models.py`** — add the field with a type and default:

   ```python
   class GraphState(BaseModel):
       model_config = ConfigDict(extra="forbid")
       # … existing fields …
       your_new_field: Optional[YourType] = None
   ```

2. **`orchestration/nodes.py`** — read and/or set the field in the relevant
   node function.  Return only the changed fields in the partial state dict.

3. **`tests/test_contracts.py`** — add a test that the field is accepted and
   that an unexpected field is rejected:

   ```python
   def test_graph_state_accepts_new_field():
       event = make_order_event()
       state = GraphState(event=event, your_new_field=<value>)
       assert state.your_new_field == <value>

   def test_graph_state_rejects_unknown_fields():
       with pytest.raises(ValidationError):
           GraphState(event=make_order_event(), unknown_field="oops")
   ```

#### Adding a new top-level contract model

1. Define the model in `contracts/models.py` with `extra = "forbid"` and typed
   fields.
2. Import and use it from the module that produces or consumes it.
3. Keep separate concerns in separate models — do not add recipe-output fields
   to `ComplianceDecision` or compliance fields to `ExecutionLog`.

#### Modifying an existing contract

- Adding an optional field with a default is non-breaking.
- Removing a field or changing a type is breaking — check all callers and tests
  first.
- Renaming a field requires updating every node, test, and the observability
  tracer that references it.
- Run `python -m pytest` after every contract change; `tests/test_contracts.py`
  validates all models against their invariants.

---

## Adding a new recipe (checklist)

1. Implement the recipe function in `recipes/` (pure function, deterministic, no side effects beyond SAP writes)
2. Register it in `recipes/registry.py` — `REGISTRY` dict + required params + `expected_metadata_keys` (Guardrail #3)
3. Add the new name to `AllowedRecipeName` Literal in `constraints/specs.py`
4. Update `GuidanceRegexBackend.recipe_name_regex()` in `constraints/guidance_backend.py`
5. Add intent → recipe mapping in `constraints/fallback_backend.py`
6. Update `orchestration/nodes.py` `validate_types()` to build the `RecipeInvocation`
7. Run `python -m pytest` — the vocabulary sync tests and V1 Guardrail tests will catch any mismatch

---

## Constrained-generation boundaries

Every value that flows into a state transition or execution decision is
constrained at generation time.  Free-form text is only permitted for
human-facing `explanation` fields.

| Output | Constraint | Schema |
|---|---|---|
| Intent | `AllowedIntent` Literal | `IntentDecision` |
| Shadow verdict | `AllowedShadowStatus` Literal | `ShadowDecision` |
| Recipe name | `AllowedRecipeName` Literal + registry `KeyError` | `RecipeProposal` |
| Resolution action | `AllowedResolutionAction` Literal | Recipe output `recommended_action` |
| Recipe params | Pydantic `RecipeInvocation` + required-param check | `RecipeInvocation` |
