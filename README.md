# ASOE — Agentic Systems of Engagement
ASOE is the Agentic System of Engagement where AI agents and humans collaborate to take action across enterprise systems. AI agents diagnose root causes, resolve exceptions autonomously, and keep every stakeholder informed. A deterministic (???? - why deterministic only) compliance-first orchestration scaffold for order management related exceptions handling along with customer comminucation management, conversational chat and reporting. Built on a **Skill–Shadow–Recipe** architecture where every automated action must pass through a Compliance Shadow before execution.
---

## What this system does

When a retailer order has a price discrepancy (e.g. PO price ≠ SAP base price),
the system:

1. **Classifies intent** — constrained to `CONTRACTUAL_CORRECTION`,
   `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, or `DUPLICATE_PO` (no free-form text enters state transitions)
2. **Audits via Compliance Shadow** — returns `GREEN` / `YELLOW` / `RED`; halts
   on anything other than `GREEN`
3. **Selects a deterministic recipe** — constrained to registered names only
4. **Executes the recipe** — immutable business logic, no autonomous reasoning

No recipe runs unless intent is classified, shadow returns `GREEN`, and all
parameters are type-validated.

---

## Architecture overview

```
OrderEvent
    │
    ▼
 ingest ──► classify ──► load_skill ──► validate_circuit_breaker
                                                │
                                          (breach → FAIL_TO_HUMAN)
                                                │
                                          shadow_audit
                                                │
                                   GREEN ◄──────┤──────► YELLOW → MANUAL_REVIEW_REQUIRED
                                     │          └──────► RED    → BLOCKED
                                     ▼
                              select_recipe ──► validate_types ──► resolve_dependencies
                                                                         │
                                                                   (gw fail → FAIL_TO_HUMAN)
                                                                         │
                                                                   execute_recipe ──► apply_effects
                                                                                          │
                                                                                   COMPLETE / FAIL_TO_HUMAN
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
uv pip install "langgraph>=0.2.0" "pydantic>=2.7.0" "pytest>=8.0.0" "pytest-cov>=5.0.0"

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

Expected: **525 passed, 0 failed, 1 warning** (the warning is from `langchain_core` pydantic.v1 deprecation — not a blocker).

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

See `architecture_v2.md` §2 for the full Azure infrastructure stack.

---

## Directory structure

```
contracts/          Typed Pydantic models — OrderEvent, GraphState, ExecutionLog, …
  policy.py         Centralised business thresholds (discount limits, circuit breaker bounds, etc.)
skills/             SKILL.md files (loaded verbatim, never rewritten)
compliance/         Compliance Shadow — audit() + enforce()
constraints/        Constrained-generation schemas, backends, router
  specs.py          AllowedIntent / AllowedShadowStatus / AllowedRecipeName Literals
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
observability/      LangFuse-ready structured tracing (no langfuse import)
hardening/          Kill switch + explain mode implementation
docs/               AUDITOR_GUIDE.md
  specs/            Product-owner reference specs (not runtime code)
tests/              pytest test suite (525 tests)
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
| `architecture_v2.md` | Architecture patterns and principles: Skill–Recipe decoupling, Hexagonal Gateways, Saga workflows, execution invariants |
| `DESIGN.md` | Implementation reference: module map, class/function names, graph node wiring, env vars, container layout |
| `docs/AUDITOR_GUIDE.md` | Audit controls: constrained-generation boundaries, kill switch, explain mode, 10 execution invariants |
| `contracts/policy.py` | Centralised business thresholds — discount limits, circuit breaker bounds, credit exposure tolerance |
| `prompts/po-spec-to-asoe.md` | Step-by-step prompt for converting a Product Owner specification into ASOE Skill–Shadow–Recipe components |
| `prompts/triple_check_review_board.md` | Reusable review prompt — three-persona architecture, security, and test coverage assessment |
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
| 5 | LangFuse-ready tracing (`observability/tracer.py`) + golden regression tests |
| 6 | Kill switch, read-only explain mode, auditor docs, constrained-generation safeguard documentation |
| 7 | Infrastructure gateways (Ports & Adapters), multi-step workflows (Saga pattern), DUPLICATE_PO fallback routing |
| 8 | Local execution sandbox — SQLite seeder, Streamlit UI, LocalHFBackend (Outlines + HuggingFace) |
| 9 | Containerized deployment — 3 Dockerfiles (core/ui/inference), docker-compose for local dev, K8s manifests for AKS |
| Review | Triple-Check Technical Review Board — resolved 10 findings (1 Critical, 1 High, 8 Medium); 7 Low findings debated and accepted (SKIP); test count 490 → 525 |

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ASOE_KILL_SWITCH` | `0` | `1` / `true` / `yes` — halt all automated execution before any node runs |
| `ASOE_EXPLAIN_MODE` | `0` | `1` / `true` / `yes` — dry-run only; shadow audits but no recipe executes |
| `USE_OUTLINES_BACKEND` | `0` | `1` — use `OutlinesConstrainedBackend` (requires `pip install -e ".[outlines]"`) |
| `SANDBOX_DB_PATH` | `tests/sandbox/sandbox.db` | Path to the sandbox SQLite database |
| `LOCAL_LLM_BACKEND_CLASS` | _(unset)_ | Fully-qualified class to use as the constrained backend (e.g. `tests.sandbox.llm.local_backend.LocalHFBackend`) |
| `LOCAL_LLM_MODEL` | `Qwen/Qwen2.5-0.5B-Instruct` | HuggingFace model id for `LocalHFBackend` |
| `LOCAL_LLM_DEVICE` | `cpu` | Compute device for `LocalHFBackend` (`cpu` / `cuda` / `mps`) |
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
| `constraints/specs.py` | Defines `AllowedIntent`, `AllowedShadowStatus`, `AllowedRecipeName` Pydantic Literals and the output schemas (`IntentDecision`, `ShadowDecisionSchema`, `RecipeProposal`) |
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
| Recipe params | Pydantic `RecipeInvocation` + required-param check | `RecipeInvocation` |
