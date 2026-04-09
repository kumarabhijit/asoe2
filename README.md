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
   `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, or `DUPLICATE_PO` (no free-form text enters state transitions)
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

### Prerequisites

- **Python 3.14.3** (stable, pinned in `.python-version`)
- **uv** — fast Python package manager ([install](https://docs.astral.sh/uv/getting-started/installation/))

No GPU or optional packages required for development or testing.

### Install

```bash
# 1. Update uv
uv self update 
# 2. Install Python 3.14.3 (if not already present)
uv python install 3.14.3

# 3. Create virtual environment
uv venv --python 3.14.3

# 4. Install core + dev dependencies
uv pip install "langgraph>=0.2.0" "pydantic>=2.7.0" "fastapi>=0.110.0" "uvicorn[standard]>=0.29.0" "pytest>=8.0.0" "pytest-cov>=5.0.0" "httpx>=0.27.0"

# 5. Apply compatibility patch (pydantic 2.12.x + Python 3.14 typing API change)
bash scripts/apply-patches.sh .venv/bin/python

# 6. Activate the environment
source .venv/bin/activate
```

For the optional Outlines constrained-generation backend (GPU-heavy, not needed for CI):

```bash
uv pip install "outlines>=0.0.46" "transformers>=4.41.0" "torch>=2.3.0" "accelerate>=0.30.0" "huggingface-hub>=0.23.0"
bash scripts/apply-patches.sh .venv/bin/python   # re-run after any pydantic reinstall
```

> **Note on the pydantic patch:** Python 3.14 renamed an internal `typing._eval_type`
> parameter from `prefer_fwd_module` to `parent_fwdref`. Pydantic 2.12.5 uses the old
> name. `scripts/apply-patches.sh` applies the one-line fix in-place. It is idempotent
> and safe to re-run. The patch will become unnecessary once pydantic ships a compatible
> release. See `patches/pydantic-py314-typing-eval-type.patch` for the diff.

### Run the tests

```bash
python -m pytest
```

Expected: **718 passed, 0 failed** (a warning from `langchain_core` pydantic.v1 deprecation may appear — not a blocker).

> **Verified on Python 3.14.3 (stable).**

### Smoke test

```bash
python main.py
```

This runs a single demo `OrderEvent` through the full graph and prints the
resulting `GraphState`.  Honoured by kill switch and explain mode.

### Sandbox CLI (headless runner)

The CLI runner executes sandbox scenarios through the full pipeline and prints
execution traces to the terminal.  No browser or Streamlit required.

```bash
# 1. Seed the SQLite database (if not already done)
python tests/sandbox/seed.py

# 2. Run all 18 seeded events
PYTHONPATH=. python tests/sandbox/cli.py

# 3. Run a single event
PYTHONPATH=. python tests/sandbox/cli.py --event EVT-CC-001

# 4. Filter by intent
PYTHONPATH=. python tests/sandbox/cli.py --intent CREDIT_BLOCK

# 5. Show full JSON trace and prompt previews
PYTHONPATH=. python tests/sandbox/cli.py --event EVT-CC-001 --json --prompts

# 6. Summary only (suppress per-event traces)
PYTHONPATH=. python tests/sandbox/cli.py --quiet
```

The runner uses `DeterministicFallbackBackend` by default.  Set
`LOCAL_LLM_BACKEND_CLASS` to use a real constrained-generation model (see
environment variables below).

### Sandbox UI (local interactive visualiser)

The sandbox runs all four intents through the live pipeline and displays a
step-by-step execution trace in a browser.  It does not require a GPU or a
cloud LLM — the `DeterministicFallbackBackend` is used by default.

```bash
# 1. Install sandbox dependencies (streamlit only; outlines/transformers optional)
uv pip install streamlit

# 2. Seed the SQLite database with sample SAP pricing, contracts, and EDI events
python tests/sandbox/seed.py

# 3. Launch the UI
PYTHONPATH=. streamlit run tests/sandbox/ui/app.py
# → open http://localhost:8501
```

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

**Key endpoints:**

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/v1/health` | public | Health check + dynamic enum serving |
| `POST` | `/api/v1/exceptions/resolve` | analyst+ | Synchronous exception resolution |
| `POST` | `/api/v1/exceptions/resolve/explain` | analyst+ | Explain mode dry-run |
| `GET` | `/api/v1/exceptions` | analyst+ | Paginated exception queue |
| `PATCH` | `/api/v1/exceptions/{id}/override` | manager+ | Human override |
| `POST` | `/api/v1/workflows` | manager+ | Multi-step workflow execution |
| `PUT` | `/api/v1/policies/{tenant_id}` | admin | Policy override update |

All protected endpoints require a JWT Bearer token in the `Authorization`
header. Set `ASOE_JWT_SECRET` for production; the dev fallback is used when
unset. See `DESIGN.md` §15 for the full endpoint table and RBAC matrix.

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
3. Select an EDI event from the sidebar (18 sample events covering all 4 intents)
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
  connection.py     SQLiteAdapter / PostgresAdapter, create_adapter() factory
  repository.py     ExceptionRepository, TraceRepository, PolicyRepository
  migrations/       V001__initial_schema.sql (5 tables, RLS, SOX trigger, pgvector)
docs/               AUDITOR_GUIDE.md, ADR-001, ADR-002
  specs/            Product-owner reference specs (not runtime code)
tests/              pytest test suite (718 tests)
  sandbox/          Local execution sandbox (not part of CI test suite)
    cli.py          Headless CLI runner — run events from the terminal (no Streamlit needed)
    seed.py         SQLite seeder — creates sandbox.db with customers, DCs, promotions, SAP / EDI data
    ui/app.py       Streamlit execution-trace visualiser
    llm/local_backend.py  LocalHFBackend — Outlines + HuggingFace model (optional)
    llm/prompts.py  Human-readable prompt templates for the UI "Prompt Preview" panel
    requirements-sandbox.txt  Sandbox-only deps (streamlit, outlines, transformers, torch)
Dockerfile.core     Core orchestration container (LangGraph + recipes + shadow)
Dockerfile.ui       Streamlit sandbox UI container (core + streamlit)
Dockerfile.inference  Local LLM inference container (Outlines + torch + transformers)
docker-compose.yml      Local dev stack — core + ui + optional inference profile (local build)
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
| `docs/adr/ADR-001-core-deployment-model.md` | Library vs. service deployment decision, staged evolution triggers |
| `docs/adr/ADR-002-database-access-pattern.md` | Raw SQL vs. ORM decision, migration triggers, expert perspectives |
| `prompts/po-spec-to-asoe.md` | Step-by-step prompt for converting a Product Owner specification into ASOE Skill–Shadow–Recipe components |
| `prompts/triple_check_review_board.md` | Reusable review prompt — three-persona architecture, security, and test coverage assessment |
| `prompts/phase_10_langfuse.md` | LangFuse integration prompt — sink design, trace mapping, self-hosted setup, SDK compatibility, test plan |
| `prompts/phase_12_api_layer.md` | FastAPI API layer prompt — 19 endpoints, auth, RBAC, tenant isolation, error envelope |
| `prompts/phase_13_database_layer.md` | Database layer prompt — PostgreSQL schema, migrations, repository, RLS, SOX audit |
| `prompts/phase_14_auth_security.md` | Auth & security hardening prompt — token expiry, env isolation, trace_id, partner scoping |
| `tests/sandbox/seed.py` | Sandbox seeder: customers, DCs, promotions, SAP pricing, retailer contracts, credit profiles, and 18 EDI events covering all four intents |
| `tests/sandbox/cli.py` | Headless CLI runner — run sandbox events from the terminal without Streamlit |
| `tests/sandbox/ui/app.py` | Streamlit execution-trace visualiser — select event, run pipeline, inspect trace |

**Start here if you are:**
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

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ASOE_KILL_SWITCH` | `0` | `1` / `true` / `yes` — halt all automated execution before any node runs |
| `ASOE_EXPLAIN_MODE` | `0` | `1` / `true` / `yes` — dry-run only; shadow audits but no recipe executes |
| `ASOE_ENV` | `sandbox` | `sandbox` or `production` — JWT `env` claim must match (§11.6) |
| `ASOE_JWT_SECRET` | _(dev fallback)_ | JWT signing secret — **required for production** (Key Vault-managed) |
| `DATABASE_URL` | _(unset)_ | PostgreSQL connection string; when set, API uses database-backed store |
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
       YOUR_NEW_INTENT        = "YOUR_NEW_INTENT"   # ← add here
       UNKNOWN                = "UNKNOWN"
   ```

3. **`constraints/guidance_backend.py`** — extend the intent regex:

   ```python
   def intent_regex(self) -> str:
       return r"CONTRACTUAL_CORRECTION|CREDIT_BLOCK|MASS_PRICING_ERROR|DUPLICATE_PO|YOUR_NEW_INTENT"
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
2. Register it in `recipes/registry.py` — `REGISTRY` dict + required params
3. Add the new name to `AllowedRecipeName` Literal in `constraints/specs.py`
4. Update `GuidanceRegexBackend.recipe_name_regex()` in `constraints/guidance_backend.py`
5. Add intent → recipe mapping in `constraints/fallback_backend.py`
6. Update `orchestration/nodes.py` `validate_types()` to build the `RecipeInvocation`
7. Run `python -m pytest` — the vocabulary sync tests will catch any mismatch between `AllowedRecipeName`, `REGISTRY`, and the regex

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
