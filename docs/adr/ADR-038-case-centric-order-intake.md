# ADR-038: Case-Centric Order Intake — Five-Layer Agentic Architecture (Manual / Automated Orders)

**Status:** Proposed
**Date:** 2026-05-06
**Deciders:** Principal AI/Agentic Engineering Architect; Compliance Veto Holder; Tools Admin / SRE; Frontend Platform; Domain SME (CSR workflow); Product Owner.
**Applies to:** `contracts/models.py`, `api/store.py`, `db/migrations/`, `orchestration/`, `recipes/`, `gateways/`, `compliance/`, `skills/` → `knowledge/skills/`, `policy/`, `agents/` (NEW), `api/`, and the corresponding UI surface in `asoe-ui/`.
**Related:** ADR-021 (deployment); ADR-022 (database access); ADR-023 (disposition + hash-chained audit); ADR-024 (OM coverage); ADR-025 (gateway READS before shadow); ADR-026 (event-driven ingestion, Proposed); ADR-027 (pipeline visualization, Proposed); ADR-028 (DuplicatePO storage shape); ADR-029 (override merge policy); ADR-030 (config override hierarchy); ADR-032 (calibration deferral); ADR-033 (override reason vocabulary); ADR-034 (email-order-entry skill — **§6 superseded by this ADR**); **ADR-039 (LLM Compliance Shadow second-opinion, companion to this ADR)**.
**Supersedes (in part):** ADR-034 §6 ("two surfaces — `/inbox` for browse, `/exceptions` for action") replaced by §3 of this ADR.

---

## 1. Context

The codebase has evolved through ADR-021..033 as an event-driven, deterministic Skill-Shadow-Recipe system. Each inbound event (EDI 850 message, email-classifier output, scheduled scan finding) creates one `GraphState`, runs through the LangGraph topology, and produces one `ExceptionRecord`. This served the V1 scope (pricing & promotional exceptions on EDI inputs) well, but the assumptions break under three forces:

### 1.1 The case-centric workflow gap

CSR practice is **case-centric**, not event-centric. A junior associate today reads inbound prose, transcribes it into the ERP, and a senior associate resolves whichever exceptions the ERP raises — but both work on **the same business object** (the customer's order). ASOE collapses both human roles into one operator + agent. Forcing the operator to mentally re-stitch children to parents across two surfaces (`/inbox` for the email, `/exceptions` for each exception the email produced) is a workflow tax paid for an architectural taxonomy the user does not share.

ADR-034 §6 attempted to address this with a "unified detail surface" (Phase G — in flight on `claude/review-order-entry-architecture-RCIUa`). On reflection, that was a UI bandage over a missing data-model tier. The system has no first-class notion of a *case* that survives across events; we papered over the symptom by stacking the email source above the recipe section on a single screen.

### 1.2 The agentic-memory architecture gap

A second, deeper review with the Agentic Engineering perspective (Karpathy LLM-as-CPU framing; MemGPT memory hierarchy; Boris Cherny's harness experience from Claude Code; multi-agent-systems literature on coordination cost) surfaced that the IA decision was masking an **agent-memory architecture decision**:

* Today's `GraphState` is **stateless per event** — it lives for one invocation and dies. The agent has no working memory of "this is the third touchpoint on this case; we already escalated to the buyer last week."
* **Procedural memory** (recipes, policy thresholds, audit-bearing registry) is scattered across `recipes/`, `contracts/policy.py`, `compliance/audit_bearing_registry.yaml`, `constraints/specs.py` literals — implicitly a knowledge tier without being named as one.
* **Skills today are degenerate** — prose-only `SKILL.md` files. They lack examples, assets, and bundled requirement specs. Boris's load-bearing argument applies here: a rich skill library is the single largest investment that prevents reaching for multi-agent (which has well-documented coordination-cost failure modes).

### 1.3 The vocabulary gap

The codebase hardcodes `EDI_850_*` event-type prefixes (`EDI_850_DUPLICATE_PO`, `EDI_850_PRICE_HOLD`, `EDI_850_LINE_MISMATCH`) as if EDI X12 850 were the only automated channel. In reality:

* Multiple EDI document types (850, 855, 860, 870, …) can create or modify orders.
* Customer self-service portals produce structured order data without EDI X12 at all.
* API feeds, FTP-dropped CSV/XML, vendor-managed-inventory replenishment all produce orders.

The `EDI_850_*` prefix conflates **what the issue is** (duplicate PO, price hold, line mismatch) with **how it arrived** (the channel). The two are independent concerns and must be separated cleanly before the case-centric model can describe order origin honestly.

### 1.4 Why now

* ADR-034 (email-order-entry) is in flight. Phase A/B/G have shipped on a feature branch but not merged. Pausing to land the case-centric foundation prevents the Phase G UI bandage from calcifying into a load-bearing assumption.
* The PO has explicitly approved the case-centric direction, the Manual / Automated Order vocabulary, and the channel-neutral issue naming.
* The agentic-engineering review (with Karpathy's, MemGPT's, multi-agent-systems', Boris Cherny's, Production-SRE's, and Compliance's perspectives) has converged on a coherent five-layer model that rests cleanly on top of what already exists.

---

## 2. Decision (summary; sections that follow expand each point)

This ADR establishes seven binding decisions:

1. **Adopt a five-layer architecture** with **L0 (Knowledge) as a first-class tier** alongside L1 (Deterministic Primitives), L2 (Bounded LLM Primitives), L3 (Case Agent), and L4 (Harness). See §4.

2. **Promote SKILL.md from prose-only "brain" to a versioned bundle** under `knowledge/skills/<name>/` containing `SKILL.md`, `examples/`, `assets/`, `specs/`, and a `metadata.yaml` manifest. The bundle is a deployable artifact governed by CODEOWNERS and stamped on every audit-log record. See §5.

3. **Introduce `OrderCase` as the parent entity** for the agent's reasoning across time. Every Manual Order opens a case at email arrival; every Automated Order opens a case **lazily** — on the first non-clean event (any `MANUAL_REVIEW_REQUIRED` / `BLOCKED` / `FAIL_TO_HUMAN` outcome). See §6, §7.

4. **Adopt the vocabulary `case.source ∈ {"manual_order", "automated_order"}`** — Manual Orders are channels where a human composes free-form input that a CSR must read (email, phone, fax). Automated Orders are channels where structured order data arrives without human prose composition (any EDI X12 document, customer self-service portal, API feed, FTP drop, VMI replenishment). The PO's binding rule: **portals are Automated**, even though a buyer typed them, because the CSR has no prose to extract. See §3.

5. **Adopt channel-neutral issue/intent naming.** `EDI_850_*` event-type strings remain as **legacy aliases** for backward compatibility but are deprecated; new code emits channel-neutral names. The `EDI_MISMATCH` intent is renamed `LINE_MISMATCH` (with `EDI_MISMATCH` retained as an alias). The channel lives on `case.source`; never in the issue name. See §3, §9.

6. **Introduce a tiered case materialisation policy** (Tier 1 stateless / Tier 2 stateful / Tier 3 compacted) so high-volume clean automated traffic does not pay the cost of case storage and agent reasoning, while complex multi-touchpoint cases get the full agent-memory hierarchy they need. See §7.

7. **Run the Case Agent as a single LLM-driven coordinator** above the existing deterministic graph. The agent decides which deterministic primitives to invoke for the current event; primitives themselves remain compliance-bound (constrained generation, audit-bearing registry, Compliance Shadow). The fourth LLM call introduced (case-level judgment) is bounded by the **tool surface**, not by a constrained-output schema — Boris's harness-driven model. See §6.

ADR-039 (companion document) extends the Compliance Shadow with a constrained-output local LLM second opinion that may **downgrade** verdicts but never **upgrade** them — preserving the existing deterministic floor while adding a judgment-tier safety net for cases where rule-based Shadow's coverage is insufficient.

---

## 3. Vocabulary (binding for the codebase)

### 3.1 Order source (case-level, set at case open, immutable)

| Term | Definition | Examples |
|---|---|---|
| **Manual Order** | An order whose inbound channel was a human composing free-form prose that a CSR has to read. | Email; phone call; fax. |
| **Automated Order** | An order whose inbound channel produces structured order data without human prose composition. The criterion is "no CSR prose extraction at intake." | EDI X12 (any document type — 850, 855, 860, 870, …); customer self-service portal; API feeds; FTP CSV/XML drops; vendor-managed-inventory replenishment files. |

**The criterion is workload-centric, not technology-centric.** A portal where the buyer types the order is **Automated** because the portal already produced structured data; the CSR has no prose to extract. This matches the actual exception risk profile: portal-typed orders share extraction-error patterns with EDI feeds (typos, code mismatches), not with email orders (free-text ambiguity, ship-to dictated in cover-letter prose).

### 3.2 Issue / intent naming (channel-neutral)

Issue and intent names describe **what the problem is**, never **how it arrived**:

| Today (legacy alias retained) | Going forward |
|---|---|
| `EDI_850_DUPLICATE_PO` (event_type) | `DUPLICATE_PO_DETECTED` |
| `EDI_850_PRICE_HOLD` (event_type) | `PRICE_HOLD_RAISED` |
| `EDI_850_LINE_MISMATCH` (event_type) | `LINE_MISMATCH_DETECTED` |
| `EDI_MISMATCH` (Intent enum) | `LINE_MISMATCH` (with `EDI_MISMATCH` retained as alias for ≥1 release) |
| `EDI_850_PRICE_MISMATCH` (sub_type) | `PRICE_MISMATCH` (already cleanly named in `AllowedEdiMismatchSubType`; the leakage is in the parent `event_type`) |

**The legacy-alias rule:** existing `EDI_850_*` strings continue to route correctly via `skills/loader.py::select_for_event` and `constraints/fallback_backend.py::classify_intent`. New ingest paths and new tests use the channel-neutral names. The cleanup is **organic** — when a recipe is touched for an unrelated reason, its event-type strings are migrated as part of that change. No big-bang rename.

### 3.3 Channel sub-classification (carried as case metadata)

Within each `source`, a finer-grained `source_channel` enum captures the actual channel for audit:

```python
# Manual Order channels
"email" | "phone" | "fax"

# Automated Order channels
"edi_x12_850" | "edi_x12_855" | "edi_x12_860" | "edi_x12_870"  # extend per real traffic
| "portal" | "api_feed" | "ftp_csv" | "ftp_xml" | "vmi_replenishment"
```

The `source` field drives architectural decisions (workflow shape, agent decomposition); `source_channel` drives audit, telemetry, and per-channel KPIs. Recipes never branch on either — they handle issues, not channels.

### 3.4 What this ADR does **not** rename

* **`Intent` enum values** other than `EDI_MISMATCH` — unchanged. `DUPLICATE_PO`, `PRICE_HOLD_RELEASE`, `BACK_ORDER`, `OVER_MAX`, `MIN_ORDER_QTY`, `PALLET_CONFIG`, `DELIVERY_DELAY`, `CREDIT_BLOCK`, `MASS_PRICING_ERROR`, `CONTRACTUAL_CORRECTION` are already channel-neutral. (`EMAIL_ORDER_ENTRY` from the in-flight ADR-034 work is technically channel-bearing; we'll address that in §10.)
* **Recipe filenames** — unchanged. Recipes are issue-specific; the filenames already reflect that.
* **Audit-bearing registry classes** — unchanged. They mirror Pydantic class names which are already channel-neutral.

### 3.5 Why this naming matters for the agent

Boris's argument applies directly: **tool docstrings are the agent's effective behaviour spec.** When the agent reads its tool surface, channel-neutral issue names ("check duplicate PO") let it reason about the issue without first having to map "EDI_850" → "is this an EDI thing or a generic thing?" The naming cleanup *also* removes a class of LLM prompt leakage where the agent infers spurious channel-specific behaviour from intent names that happen to mention a channel.

---

## 4. The Five-Layer Architecture

The architecture is described as five layers because each tier has a distinct **lifecycle**, **determinism property**, **governance owner**, and **failure mode**. Conflating any two of them into a single layer (which the codebase implicitly does today between knowledge and code) leads directly to the gaps §1 documents.

### 4.1 Layer-by-layer summary

| Layer | Name | What it holds | Determinism | LLM involvement | Governance | Lifecycle |
|---|---|---|---|---|---|---|
| **L0** | **Knowledge** | Skill bundles (`SKILL.md` + examples + assets + specs), `audit_bearing_registry.yaml`, policy thresholds, override-reason vocabularies, customer-tier matrices, channel sub-classification enums | Pure data; deterministic | Read by L2/L3, never executed by it | CODEOWNERS-gated; Compliance + domain SME approval; bundle-versioned | Independently deployable; versioned per bundle |
| **L1** | **Deterministic primitives** | Recipes (`recipes/*.py`), gateways (`gateways/*.py`), Compliance Shadow rules (`compliance/shadow.py`), recipe registry, audit-coverage gates | Pure code | None directly | Engineering CODEOWNERS + Compliance for shadow.py and audit registry consumption | Code release |
| **L2** | **Bounded LLM primitives** | Intent classifier, attachment extractor (multimodal pipeline), buyer-email drafter (server-side templated), constrained-generation backends (Guidance/Outlines/fallback) | Effectively deterministic given constrained output + cache | Yes — narrow context per call, structured output | Engineering CODEOWNERS + Compliance for prompt templates | Code release; model version pinning |
| **L3** | **Case Agent** | The agent loop that coordinates L1+L2 invocations across the case lifetime; tool surface (~18 tools); working-memory loader; case-level judgment | Non-deterministic LLM judgment, but tool-trace deterministically logged | Yes — the agent loop driving tool calls | Engineering CODEOWNERS + Compliance for the system prompt + tool surface | Code release; agent system-prompt versioning |
| **L4** | **Harness** | The `while`-loop, budget enforcement (tokens/iterations/wall-clock/$), tool-call interception for replay, compaction trigger, concurrency control on the case, persistence, observability | Pure code | None | Engineering CODEOWNERS | Code release |

### 4.2 Interaction topology (not a dependency hierarchy)

```
                        ┌─────────────────────────┐
                        │  L4  Harness            │
                        │  (loop, budgets, persist)│
                        └─────────┬───────────────┘
                                  │ drives
                                  ▼
                        ┌─────────────────────────┐
                        │  L3  Case Agent         │ ──reads── ┐
                        │  (LLM-driven coordinator)│           │
                        └──┬──────────────────┬───┘           │
                           │ calls            │ calls         │
                           ▼                  ▼               │
                   ┌──────────────┐  ┌──────────────┐         │
                   │ L1  Det.     │  │ L2  Bounded  │         │
                   │ Primitives   │  │ LLM Prims    │         │
                   │ (recipes,    │  │ (classifier, │         │
                   │  gateways,   │  │  extractor,  │         │
                   │  shadow)     │  │  drafter)    │         │
                   └──────┬───────┘  └──────┬───────┘         │
                          │                 │                 │
                          └────────┬────────┘─────reads───────┘
                                   ▼
                        ┌─────────────────────────┐
                        │  L0  Knowledge          │
                        │  (skill bundles,        │
                        │   policy, audit-registry,│
                        │   vocabularies)         │
                        └─────────────────────────┘
```

**Reading order:**

* **L4 → L3:** the harness loads case state, builds working memory, calls the Case Agent with the current event. The agent runs its loop; the harness intercepts every tool call for replay/observability.
* **L3 → L1, L2:** the agent invokes primitives as tools. L1 calls are deterministic (recipes / gateways / shadow); L2 calls are bounded LLM operations (classify, extract, draft) with constrained outputs.
* **L3 → L0:** the agent loads the relevant skill bundle (anchor set into cached prefix; examples on-demand via tool); reads policy values; consults the audit-bearing registry for evidence-coverage decisions.
* **L1, L2 → L0:** primitives read policy thresholds, vocabulary literals, and audit-registry classifications from the same knowledge tier.

### 4.3 Determinism floor (the load-bearing property)

The **deterministic floor** is **L0 + L1 + L2-with-constrained-output**. Compliance ratifies this floor. The architecture's load-bearing safety claim:

> *L3's non-determinism is bounded by the L1+L2 tool surface. The agent picks tools but the tools themselves remain compliance-bound. No tool produces an action that has not already passed L1 (Compliance Shadow / audit-registry) gates.*

Concretely: the Case Agent cannot invent a "submit_to_erp" path that bypasses Shadow. The L4 harness intercepts the tool call, routes it through the existing graph topology (resolve_dependencies → validate_types → shadow_audit → execute_recipe → apply_effects), and the deterministic gates fire just as they do today. The agent's freedom is in *which* primitives to call and *when*, not in altering what they do.

### 4.4 What is genuinely new (vs reorganisation of what exists)

| Layer | What exists today | What is new |
|---|---|---|
| **L0** | `compliance/audit_bearing_registry.yaml`, `contracts/policy.py` constants, `constraints/specs.py` literals, `skills/<name>_SKILL.md` (prose only) | Skill bundle directory structure (`knowledge/skills/<name>/` with `examples/`, `assets/`, `specs/`, `metadata.yaml`); migration of policy constants to `policy/<intent>.yaml` (deferred, optional, see §9); examples-as-CI-tests |
| **L1** | All 10 existing recipes; gateways; `compliance/shadow.py` deterministic rules; recipe registry | No structural change. New recipes ship as new tools. |
| **L2** | Intent classifier (`skills/intent_classifier.py`); constrained-generation backends (`constraints/`); `EmailOrderEntryRecipe` extraction (in-flight on ADR-034 branch) | Attachment extractor (multimodal pipeline — PDF / OCR / Excel / image dispatch with template-fingerprint caching); buyer-email drafter (server-side templated) |
| **L3** | None | The Case Agent module (`agents/case_agent.py`); tool surface (`agents/case_tools.py`); working-memory loader (`agents/working_memory.py`) |
| **L4** | LangGraph topology (`orchestration/graph.py`, `nodes.py`); recipe executor; gateway executor; HTTP API | Case-aware extensions: `OrderCase` parent persistence; correlation-table lookup-or-create; tier graduation; compaction trigger; budget enforcement at the case-tier granularity |

### 4.5 Why five layers, not four or six

* **Why L0 is its own layer (not folded into L1 or L3):** Knowledge has its own lifecycle (versioned, CODEOWNERS-gated, deployable independently of code), its own governance (Compliance + domain SME, not engineering alone), and its own access pattern (loaded on demand by L2/L3, not invoked as code). Folding it into L1 conflates pure data with pure code; folding it into L3 makes the agent's prompt indistinguishable from policy. Both blur the audit-trail story.
* **Why L1 and L2 stay separate (not collapsed into "primitives"):** they have different determinism guarantees and therefore different audit treatment. L1 is reproducible bit-for-bit; L2 is reproducible only modulo model + cache + temperature. Compliance treats them differently. Putting them in one layer hides that distinction.
* **Why L3 and L4 stay separate (not collapsed into "runtime"):** the harness is testable in isolation with the LLM mocked; the agent is testable in isolation with the harness mocked. Replay works because L4 records inputs/outputs at the L3↔L1/L2 boundary deterministically. Conflating them defeats both tests.
* **Why not a sixth layer for "UI / API surface":** the API surface is part of L4 (the harness's external contract). The UI is a separate repository (`asoe-ui`) with its own architectural document; it consumes L4's API and doesn't sit on top of the agent stack.

### 4.6 What this layering buys us (vs the current implicit structure)

1. **Audit-trail granularity.** Every record carries (skill bundle version, policy version, audit-registry version, recipe version, agent system-prompt version, harness version). Replay against any historical version reproduces the decision.
2. **Independent evolution.** Knowledge can change (vocabulary, thresholds, examples) without a code release. Code can change without a knowledge migration. The two coordination loops are decoupled.
3. **Cost discipline.** L0's stable prefix caches; L2's bounded calls are predictable; L3's per-iteration budget is enforceable; L4 measures the whole.
4. **Compliance posture.** The deterministic floor (L0+L1+L2) is what's ratified. L3's non-determinism is bounded *by* the floor, not above it.
5. **Modularity.** New capabilities ship by adding a skill bundle (L0) + recipe (L1) + maybe a primitive (L2). The agent (L3) and harness (L4) don't change. This is the "richer skill library beats more agents" property Boris emphasises.

---

## 5. L0 Knowledge Layer — Skill Bundles, Examples, Assets, Specs

L0 is the architectural shift this ADR is most invested in. The codebase already *has* a knowledge tier; it's just scattered (prose-only `SKILL.md`, `policy.py` constants, `audit_bearing_registry.yaml`, `constraints/specs.py` literals). This section formalises the tier with one canonical home (`knowledge/`), one bundle structure per skill, and one set of loading rules that interact correctly with prompt caching.

### 5.1 The bundle directory (canonical home)

```
knowledge/
  skills/
    email-order-entry/
      SKILL.md                        # reasoning guide (prose; existing role, new home)
      metadata.yaml                   # bundle manifest (NEW)
      examples/                       # few-shot exemplars (NEW)
        clean_one_click.example.json
        ambiguous_ship_to.example.json
        missing_delivery_date.example.json
        sender_unauthorized.example.json
      assets/                         # server-rendered templates + reference docs (NEW)
        clarification_ship_to.template.md
        clarification_delivery_date.template.md
        reject_sender_unauthorized.template.md
        customer_tier_response_matrix.reference.md
      specs/                          # raw requirement specs (NEW; runtime:false)
        order_entry_spec.md           # the original PO spec preserved verbatim
      tests/                          # examples-as-CI-tests
        examples_match_recipe.test.py
    duplicate-po/
      SKILL.md
      metadata.yaml
      examples/
      assets/
      specs/
        duplicate-po-product-spec.md  # migrated from docs/specs/
        calibration-methodology.md    # migrated from docs/specs/duplicate-po/
      tests/
    [...one bundle per skill]
  policy/                             # OPTIONAL future home for policy.py constants (deferred — see §9)
  audit_bearing_registry.yaml         # symlinked or moved from compliance/ (deferred)
```

**Why `knowledge/` and not `skills/`:** the existing `skills/` directory in the repo root is **code** (`skills/loader.py`, `skills/intent_classifier.py`). Putting bundles under the same name would conflate L0 (data) with L4 (loader code). `knowledge/skills/` keeps the L0 root distinct, and following the PO's confirmed direction extends naturally to `knowledge/policy/`, `knowledge/audit_registry/`, etc., as those tiers migrate.

### 5.2 `metadata.yaml` schema (the manifest)

```yaml
schema_version: 1
skill_name: email-order-entry
bundle_version: 2.0.0                 # SemVer; major bump for any reasoning-guide change
recipes: [EmailOrderEntryRecipe.py]   # L1 primitive(s) this skill calls
intents: [EMAIL_ORDER_ENTRY]          # L0 vocabulary (constraints/specs.py AllowedIntent)
event_types:                          # routing keys (skills/loader.py uses these)
  - EMAIL_ORDER_ENTRY_REQUEST
  - EMAIL_ORDER                       # legacy alias

# Anchor examples — loaded into the cached prefix on every call to this skill.
# Keep small (1-2). Change rarely (CODEOWNERS-gated). These pay for themselves
# on cache hits across the case lifetime.
anchor_examples:
  - file: examples/clean_one_click.example.json
    purpose: "Representative ONE_CLICK_APPROVE for the agent's prior."

# On-demand examples — agent loads via load_example(skill, name) tool.
# Manifest entries below are loaded as one-line summaries (NOT bodies).
on_demand_examples:
  - file: examples/ambiguous_ship_to.example.json
    summary: "STANDARD_REVIEW with REQUEST_CLARIFICATION on ship-to ambiguity"
    classification: STANDARD_REVIEW
  - file: examples/missing_delivery_date.example.json
    summary: "STANDARD_REVIEW with REQUEST_CLARIFICATION on missing date"
    classification: STANDARD_REVIEW
  - file: examples/sender_unauthorized.example.json
    summary: "FATAL_REJECT with reject_reason_code=sender_unauthorized"
    classification: FATAL_REJECT

# Server-rendered templates the agent invokes via draft_using_template(skill, template_name, fields).
# Body never enters the agent's context (Boris: templates are syscalls, not data).
assets:
  - file: assets/clarification_ship_to.template.md
    role: buyer_email_draft
    triggered_by: "recommended_action == REQUEST_CLARIFICATION AND validation_failures contains 'ambiguous_ship_to'"
  - file: assets/reject_sender_unauthorized.template.md
    role: buyer_email_draft
    triggered_by: "classification == FATAL_REJECT AND reject_reason_code == 'sender_unauthorized'"

# Specs — preserved for human review; NEVER loaded into the agent's runtime context.
runtime_includes:                     # explicit allowlist for the loader
  - SKILL.md
  - metadata.yaml
  - examples/*.example.json           # bodies loaded via load_example tool only
  # specs/ deliberately absent — humans navigate to them; agent never sees them.

# Cost guardrails (enforced by L4 harness during bundle load).
token_budget:
  cached_prefix_max_tokens: 3000      # SKILL.md + metadata.yaml + anchor_examples bodies
  on_demand_load_max_tokens: 4000     # per single load_example call
```

**The `runtime_includes` allowlist is load-bearing for safety.** The L4 bundle loader hard-fails if a request reads a path not in the allowlist. Specs accidentally included would leak proprietary requirement language into the agent's prompt; the allowlist prevents this by design, not by convention.

### 5.3 Loading semantics (the Karpathy cache-discipline rules)

**Stable cached prefix (per skill, ~3k tokens, paid once per cache TTL):**
1. L4 harness system instructions (small, stable)
2. The active skill's `SKILL.md` (selected per event-type, stable for the case lifetime)
3. The active skill's `anchor_examples` bodies (1–2 examples, stable, change rarely)
4. The on-demand example **manifest** (one-line summaries only, no bodies)

**Per-turn (not cached):**
5. Case working memory — compacted summary + last N actions
6. Current event payload
7. Tool-call history this turn

**On-demand (loaded by the agent via tool calls; one-time pay per load):**
8. `load_example(skill, name)` — pulls one example body into context for this turn
9. `draft_using_template(skill, template_name, fields)` — renders server-side; the body never enters context, only the rendered draft does

This ordering is **not aesthetic**. Item-1 must be first, item-3 must precede item-4, etc., because every position-shift past the cache boundary invalidates the cached prefix. The L4 harness enforces the order in `agents/working_memory.py`; CI fails if the order regresses.

### 5.4 The five Karpathy cost-discipline rules (binding)

These five rules govern bundle authoring and loading. The `metadata.yaml` schema and L4 loader together enforce them:

1. **Anchor set is small and stable.** Per skill: 1–2 examples maximum, chosen for representativeness, changed rarely (once per quarter, not weekly). They ride in the cached prefix. CI fails if a bundle ships >2 anchors.
2. **On-demand examples are loaded via tool call.** Manifest entries carry one-line summaries so the agent doesn't have to load to discover. `load_example` is a budget event the harness logs.
3. **Specs are `runtime: false`** — surfaced through the `runtime_includes` allowlist. The loader hard-fails if a request escapes the allowlist. Defence in depth.
4. **Per-skill token-cost budget.** `metadata.yaml::token_budget.cached_prefix_max_tokens = 3000`. Bundle-load CI test asserts the budget. Adding a 5k-token SKILL.md fails the build.
5. **Cache-hit-rate monitoring.** Prometheus metric per skill on prefix-cache hit rate. If it drops below 70% for a skill, that's a regression — investigate. Probable cause: someone edited an anchor example in a hot loop. SLI alarm + PagerDuty when sustained.

### 5.5 Honest answer to the cost / accuracy concern (the question raised in our review)

**Will Karpathy discipline alone solve cost-bloat and accuracy-from-context-bloat? Mostly yes, with caveats:**

* **Cost discipline is necessary but not sufficient** without empirical validation. Examples can degrade reasoning if they're not representative (the LLM-anchoring-bias risk). The discipline must be paired with **per-example A/B testing** in CI: for each example, run a hold-out test set with and without that example loaded; measure classification F1 on the hold-out. Drop examples whose lift is < 1%. This is non-negotiable bundle-authorship discipline; without it, examples accrete and the cost-vs-lift curve goes inverted within months.
* **Cost outcome with discipline:** cold-start cases pay ~+$0.01–0.02 per case (extraction + first-event reasoning gains anchor-example tokens). Long-running cases pay *less* than today because cached anchor reduces re-reading. Net at scale: roughly flat cost per case, materially higher accuracy on edge cases (ambiguous ship-to, customer-specific SKU mappings, unusual reject reasons).
* **Accuracy outcome with discipline:** noticeable lift on edge cases; unchanged on clean common cases. The few-shot mechanism doesn't help when there's no ambiguity to resolve; it helps when the model has to recognise a pattern from prior examples. For our domain, the lift surface is the long tail.
* **Without discipline:** cost balloons (loaded examples grow without measurement), cache-hit rate collapses (eager-loading invalidates prefixes), accuracy possibly *degrades* on the common case (anchoring bias to examples that don't represent it). This is the failure mode we explicitly avoid.

**Recommendation (binding for the bundle authorship process):**
1. Ship bundle structure with **empty `examples/` and `assets/`**.
2. Add an example **only when** there's a measured edge case — typically when an audit reveals a wrong-band classification on a real record.
3. The example becomes the regression test for that case.
4. CI enforces the lift threshold: examples whose A/B lift is < 1% fail the build.

This is more conservative than "build a rich library upfront" and is closer to the bias-toward-evidence Boris emphasises. Examples are *earned* by real failures, not authored speculatively.

### 5.6 Server-side asset rendering (Compliance ratification)

Customer-facing email templates are **rendered server-side**. The agent provides field values via `draft_using_template(skill, template_name, fields)`; the template body is filled by the L4 harness and returned as a typed `BuyerEmailDraft` object. The draft *never* sends without human approval (see §6.4 on the case-agent tool surface).

**Why server-side rendering:**

| Risk | If LLM composes the email freely | If template is server-side |
|---|---|---|
| **Brand voice** | LLM picks tone, vocabulary, style — drift over time | Template encodes brand voice; only field values vary |
| **Prompt injection** | A malicious customer email could prompt the LLM to compose abusive content; the agent's reply gets sent | Template body never enters the LLM's prompt, so it can't be hijacked |
| **Compliance review** | Every generated email needs review; auditor cannot batch-approve patterns | Template approved once; field-substitution is a small, reviewable surface |
| **Replayability** | Different LLM output each time | Same template + same fields → same draft, deterministic |

Compliance has explicit veto on customer-facing template content. CODEOWNERS rule on `assets/*.template.md` requires Compliance + brand sign-off.

### 5.7 Examples-as-CI-tests (the operational discipline)

Every example in `examples/` is a **living regression test**:

```python
# tests/<skill>/examples_match_recipe.test.py
def test_clean_one_click_example_classifies_correctly():
    example = load_example("knowledge/skills/email-order-entry/examples/clean_one_click.example.json")
    # Render through the deterministic recipe under test (NO agent involvement)
    output = classify_email_order_entry(**example["recipe_inputs"])
    assert output["classification"] == example["expected_classification"]
    assert output["recommended_action"] == example["expected_recommended_action"]
```

Adding an example that doesn't match what the recipe actually produces fails the build. Editing the recipe in a way that diverges from the examples fails the build. The two move in lock-step; bundle-version + recipe-version both bump.

This is the operational property that turns examples from "documentation that rots" into "regression tests that catch drift."

### 5.8 Tenant isolation for extraction caches (PO answer to Q5)

The L2 attachment-extractor caches by template-fingerprint. The cache **scopes to tenant** even if templates are byte-identical across tenants. Rationale:

* Multi-tenant data isolation is a non-negotiable SOX requirement — tenants must not be able to read each other's extracted data, even via cache side-channels.
* Template overlap across tenants is a coincidence, not a sharing opportunity. Treating it otherwise risks a class of bugs where Tenant A's extracted-customer-PO-number leaks into Tenant B's same-template-shape document.
* Cache-key includes `tenant_id`. Storage is partitioned per tenant.

The cost cost of this discipline is real (lower cache hit rate cross-tenant) but the safety property is non-negotiable.

### 5.9 Bundle versioning and audit-log inclusion

* Every L0 bundle has a SemVer (`bundle_version` in metadata.yaml).
* Major version bumps require Compliance workshop (any reasoning-guide change).
* Minor version bumps require CODEOWNERS approval (new examples, new assets).
* Patch version bumps for typos / clarifications.
* **Every audit-log record carries `skill_bundle_version`** alongside the existing `skill_name`. Replay against a historical version reproduces the decision against the *prior* skill content.
* Compliance audit query "what did the system do for this customer last quarter" can replay against the L0 bundle that was active at that time.

### 5.10 What this section does **not** decide

Two L0-adjacent migrations are flagged as **deferred** (see §9 for migration phases):

* **`policy.py` → `policy/<intent>.yaml` migration.** Useful eventually; not in scope for this ADR. The constants stay in `policy.py` for now; a follow-up ADR can stage the YAML migration when it earns its keep.
* **`compliance/audit_bearing_registry.yaml` → `knowledge/audit_bearing_registry.yaml` move.** Cosmetic. The file already lives where Compliance owns it (`compliance/`). Moving it under `knowledge/` is unnecessary churn unless we discover a concrete reason to consolidate.

Both are explicitly in-scope for **future** L0 work but **out of scope** for this ADR's first migration.

---

## 6. OrderCase Entity and L3 Case Agent

### 6.1 The `OrderCase` parent entity (data model)

```python
# contracts/models.py — additions
class OrderCase(BaseModel):
    """Parent record for all events / actions on a single business order.

    Created lazily for Automated Orders (on first non-clean event) and
    eagerly for Manual Orders (on email arrival). Holds the SLA clock,
    the case-source classification, and the correlation keys that let
    incoming events resolve to an existing case via lookup-or-create.
    """
    model_config = ConfigDict(extra="forbid")

    case_id: str                      # UUID; primary key
    tenant_id: str
    customer_id: Optional[str]        # may be None pre-resolution

    # Case source — set at case open, immutable.
    source: Literal["manual_order", "automated_order"]
    source_channel: str               # "email" | "edi_x12_850" | "portal" | ...

    # Correlation keys — lookup-or-create resolves incoming events to an
    # existing case via any of these. First event populates whichever it
    # has; subsequent resolutions enrich the set.
    customer_po_number: Optional[str]
    sales_order_id: Optional[str]
    edi_transaction_id: Optional[str]
    source_email_id: Optional[str]

    # Lifecycle.
    opened_at: str                    # ISO-8601
    closed_at: Optional[str]
    status: Literal[
        "OPEN_AGENT_PROCESSING",      # agent is currently working
        "OPEN_AWAITING_HUMAN",        # MANUAL_REVIEW_REQUIRED on a child
        "OPEN_AWAITING_BUYER",        # REQUEST_CLARIFICATION sent
        "OPEN_AWAITING_ERP",          # submitted; waiting on ERP confirmation
        "RESOLVED",                   # all children terminal-closed
        "FAILED",                     # case-level FAIL_TO_HUMAN
        "BLOCKED",                    # case-level RED verdict
    ]
    sla_deadline: Optional[str]       # ISO-8601; per customer-tier policy

    # Tier (see §7 for the materialisation policy).
    tier: Literal[1, 2, 3]            # T1 stateless / T2 stateful / T3 compacted

    # Children — exception records linked to this case. Materialised at
    # query time; not stored as a foreign-key list on the case row to
    # keep writes O(1).
    # (Not a Pydantic field; resolved via `select * from exception_record where parent_case_id = ?`.)

    # Working-memory pointers — what's in the agent's context vs persisted-only.
    working_memory_summary: Optional[str]   # compacted summary of episodic events
    last_compaction_at: Optional[str]
    bundle_version_at_open: Optional[str]   # L0 bundle version when case opened (audit)

# Existing ExceptionRecord gains:
class ExceptionRecord(BaseModel):
    # ... existing fields ...
    parent_case_id: Optional[str]     # NEW; foreign key to OrderCase.case_id.
                                      # Optional during Tier-1 stateless path
                                      # (clean automated orders that never
                                      # materialise a case). Populated for
                                      # every Tier-2/3 record.
```

### 6.2 Correlation table (lookup-or-create policy — answers PO Q1, Q2)

```sql
-- db/migrations/V009__order_case_correlation.sql (new)
CREATE TABLE case_correlation_keys (
  tenant_id          TEXT NOT NULL,
  key_type           TEXT NOT NULL,    -- 'customer_po' | 'sales_order_id' | 'source_email_id' | 'edi_transaction_id'
  key_value          TEXT NOT NULL,
  case_id            TEXT NOT NULL REFERENCES order_case(case_id),
  registered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key_type, key_value)
);
CREATE INDEX idx_case_corr_case_id ON case_correlation_keys (case_id);
```

**Resolution policy (binding):**

1. On every inbound event (Automated EDI, customer email, scheduled scan finding), the harness extracts whichever correlation keys are present (`customer_po_number` if known; `sales_order_id` if the event is post-ERP; `source_email_id` for emails; `edi_transaction_id` for EDI traffic).
2. The harness performs a lookup against `case_correlation_keys` for each key in the order: `sales_order_id` → `customer_po` → `edi_transaction_id` → `source_email_id`. **First match wins.** Case is bound.
3. If no key matches, the harness opens a new case (subject to the tier-graduation policy in §7) and registers each known correlation key.
4. As the case progresses and new identifiers appear (e.g., ERP creates the SO), they're registered to the existing `case_id` — never opening a new case.
5. **An event mentioning multiple `customer_po`s** (a multi-PO email) opens / attaches to **multiple cases**, one per PO. This is the corner case the PO acknowledged.

**The first-event-wins rule has one explicit exception:** if a Manual Order email's customer_po later matches a pre-existing Automated Order case (the EDI feed already opened a case for the same PO), the email attaches to the EDI case rather than opening a new one. The case `source` doesn't change (it stays whatever opened the case first), but `source_channel` becomes a list to reflect the actual touchpoints. This preserves the "one case per business order" invariant.

### 6.3 The Case Agent — Boris's `while`-loop, applied

```python
# agents/case_agent.py — the entire harness-driven agent loop, conceptually:

def run_case_agent(
    case: OrderCase,
    triggering_event: dict,
    budget: CaseBudget,
) -> AgentRunResult:
    """L3 Case Agent — coordinates L1+L2 primitives across one event.

    The agent's ONLY non-deterministic role is choosing which tools to
    call. Tools themselves are L1 (deterministic) or L2 (constrained-
    output LLM). Compliance Shadow runs inside the tool wrapper for any
    action-emitting tool; the agent cannot emit an action that bypasses
    Shadow (L4 enforces this).
    """
    while not budget.exhausted():
        ctx = working_memory.build(case, triggering_event)
        response = llm.invoke(
            system=CASE_AGENT_SYSTEM_PROMPT,        # stable, cached
            tools=CASE_AGENT_TOOLS,                 # ~18 tools, stable
            messages=ctx,                           # per-turn working memory
            max_tokens=budget.remaining_output_budget(),
        )
        for tool_call in response.tool_calls:
            result = invoke_tool_with_compliance_gates(tool_call, case)
            case.append_event(tool_call, result)    # persist to episodic memory
            budget.deduct(tool_call.cost)

        if response.declares_done():
            return AgentRunResult.SUCCESS
        if response.escalates():
            return AgentRunResult.ESCALATED(response.reason)
        if response.requires_buyer_clarification():
            return AgentRunResult.AWAITING_BUYER

    # Budget exhausted — escalate rather than running unbounded.
    return AgentRunResult.ESCALATED("budget_exhausted")
```

**Boris's seven properties, mapped to this loop:**

1. **It IS a `while` loop.** No framework. No state machine within the agent. The state machine is L4's graph, which the agent calls into via tools.
2. **Tools are the API contract.** §6.4 below.
3. **Filesystem / external state is the memory.** `case.append_event()` writes to Postgres immediately; the agent re-reads via `read_case_history` tool, never via in-LLM memory.
4. **Subagents are a last resort.** None proposed in this ADR. Future tools that need parallelism (e.g., extracting multiple attachments concurrently) are background-Tasks, not subagents — they don't have their own LLM.
5. **Harness enforces budgets.** `CaseBudget` is L4; the agent operates as if budget is infinite.
6. **Reproducibility through replay.** Every `tool_call` + `result` is logged with inputs, outputs, latency, cost, model id. Tool trace is deterministically replayable; the LLM call itself is not bit-for-bit reproducible but is replayable modulo model + temperature 0.
7. **Errors flow back as observations.** `invoke_tool_with_compliance_gates` returns structured failure objects (timeout / unavailable / shadow_red) the agent can reason about. Failures don't crash the loop.

### 6.4 The tool surface (~18 tools, the agent's effective capability set)

Grouped by purpose. **Each tool is a typed function with a structured input schema and a structured output schema.** Boris's "tools are the API contract" rule applies: the docstring + signature is what the agent sees and what shapes its behaviour.

#### Reading the case (memory access)

| Tool | Signature | Purpose |
|---|---|---|
| `read_case_summary()` | `() → CaseSummary` | Compacted overview of the case; always cheap (~200 tokens). |
| `read_case_events(filter, max_n=20)` | `(filter, max_n) → list[CaseEvent]` | Episodic memory retrieval. Filter by event type, action type, time range. |
| `read_extracted_fields(attachment_id?)` | `(attachment_id?) → ExtractedFields` | Already-extracted structured data from prior `extract_attachment` calls. Cache hit, no re-extraction. |

#### Extraction (L2 multimodal primitive wrapped)

| Tool | Signature | Purpose |
|---|---|---|
| `extract_attachment(attachment_id, fields_hint?)` | `(attachment_id, hints?) → ExtractedFields` | Returns structured fields + per-field confidence. Internally template-fingerprints, dispatches to native-PDF / OCR / Excel / multimodal-image path. Persists `attachment_processed` event. |

#### Resolution (L1 wraps)

| Tool | Signature | Purpose |
|---|---|---|
| `resolve_customer(sender_address)` | `(sender) → CustomerResolution` | Maps email-from / EDI-partner / portal-account to internal customer_id. |
| `resolve_material(customer_sku, customer_id)` | `(cust_sku, customer_id) → MaterialResolution` | Customer-SKU → internal SKU mapping with fuzzy fallback. |
| `resolve_ship_to(address_text, customer_id)` | `(addr, customer_id) → ShipToResolution` | Disambiguates ship-to addresses against the customer's known DCs. |

#### Validation (L1 recipes — issue-specific, channel-neutral)

| Tool | Signature | Purpose |
|---|---|---|
| `check_credit(customer_id, order_value)` | | Credit availability. Wraps existing `CreditHoldReleaseRecipe` logic. |
| `check_atp(lines, requested_eta?)` | | ATP availability. New tool wrapping inventory-snapshot gateway. |
| `check_duplicate_po(customer_id, po_number)` | | Duplicate-PO pre-check. Wraps existing `DuplicatePORecipe`. |
| `check_moq(lines)` | | MOQ violation check. Wraps existing `MOQRoundUpRecipe`. |
| `check_pricing_variance(customer_id, lines)` | | Pricing variance. Wraps `PriceAdjustmentRecipe` / `PriceHoldReleaseRecipe`. |
| `check_pallet_alignment(lines)` | | Pallet-config check. Wraps `PalletAlignmentRecipe`. |

#### Action (Compliance Shadow runs inside each)

| Tool | Signature | Purpose |
|---|---|---|
| `submit_to_erp(order_payload)` | | Idempotent on PO number. Compliance Shadow gates internally. |
| `apply_auto_correct(field, new_value, reason)` | | Only when confidence ≥ 0.99 (existing rule). Shadow gates. |
| `request_clarification_email(template, question_bundle)` | | **Drafts only**, does not send. Returns `BuyerEmailDraft` for human approval. Server-side templating (§5.6). |
| `escalate(reason_code, target_role)` | | Flags for human review with typed reason from L0 vocabulary. Halts the agent loop for this event. |

#### Communication (drafts only — never sends without human)

| Tool | Signature | Purpose |
|---|---|---|
| `draft_buyer_email(template, fields)` | `(skill, template, fields) → BuyerEmailDraft` | General-purpose drafting against any L0 asset template. Body never enters agent context. |

#### Memory operations

| Tool | Signature | Purpose |
|---|---|---|
| `write_case_note(note, audit_visible?)` | | Agent's working notes for future-self. Persisted to episodic memory. Optional audit visibility (see §7 on compaction). |
| `request_compaction()` | | Explicit compaction trigger when the agent decides context is heavy and a summary should be persisted. |
| `load_example(skill, name)` | | Pull one example body from the active L0 bundle into context. Manifest summaries shown in cached prefix; bodies loaded on demand. |

**Total: 18 tools.** Boris's heuristic: this is at the upper end of comfortable. Adding tools beyond this should require a strong case — the marginal tool past ~20 typically buys less capability than refining the prompts that govern the existing tools.

### 6.5 What's deliberately absent from the tool surface

* **No `send_email_to_buyer` tool.** Only `draft_*`. Sending requires the human-review surface compliance has veto over.
* **No direct DB-write tools.** The agent doesn't write to ERP / billing / CRM directly. All state changes go through `submit_to_erp` / `apply_auto_correct` / `escalate` so Compliance Shadow gates them.
* **No `delete_*` tools.** Cases are append-only. Compaction summarises; it doesn't delete.
* **No `change_intent` / `reroute_skill` tools.** Routing is L4's job; the agent works on whatever skill the harness loaded for the current event. Routing decisions go through the existing classify / select_recipe path.
* **No `spawn_subagent` tool.** Single agent by design (multi-agent failure modes documented in §1.2). Future parallelism happens via L4 task fan-out, not via agent recursion.

### 6.6 The agent's system prompt structure

```
[CACHED PREFIX — stable per skill]
  L4 system instructions (small, generic)
  ────────────────────────────────────────
  Active L0 skill bundle:
    SKILL.md (reasoning guide)
    Anchor examples (1-2 bodies)
    On-demand example manifest (one-line summaries)
  ────────────────────────────────────────
  Tool surface (the ~18 tools with structured schemas)

[PER-TURN — not cached]
  Case working memory:
    Compacted summary
    Last N actions (full detail)
    Current event payload

[AGENT RESPONSE]
  Tool calls (executed by L4 with compliance gates)
  | OR |
  Decision: done / escalate / awaiting buyer / awaiting ERP
```

The prompt structure is **the system architecture made visible to the LLM**. Stable elements ride the cache; per-turn elements pay the input cost; the tool surface is the agent's effective capability bound.

### 6.7 Concurrency and idempotency

* **One agent run per case at a time.** The case has a Postgres advisory lock; concurrent events on the same case are serialised. (The L4 harness implements this; the agent doesn't have to think about it.)
* **Idempotent tool semantics.** `submit_to_erp` is keyed by `(customer_po, idempotency_key)` so retries don't double-submit. `request_clarification_email` carries a uniqueness check on `(case_id, question_bundle_hash)` so the agent cannot send the same clarification twice.
* **Replay on crash.** If the harness crashes mid-loop, the next start re-reads the case's `last_persisted_event` and resumes from after that. The agent's reasoning gets re-derived; the persisted tool-call results don't.

---

## 7. Tier 1/2/3 Case Materialisation, Memory Hierarchy, Compaction

### 7.1 The graduation policy (the case-cost-vs-capability tradeoff, made explicit)

Not every event needs a case. A clean Automated Order — EDI 850 arrives, ATP passes, MOQ ok, pricing within tolerance, SO created — has zero ambiguity, requires zero human attention, and gains nothing from being persisted as a long-lived case with episodic memory. Forcing every clean automated event through the case-as-stateful-agent surface is an order-of-magnitude cost balloon for no operational benefit.

The graduation policy:

| Tier | Trigger | What's persisted | Agent involvement | Cost target | Latency target |
|---|---|---|---|---|---|
| **T1 — Stateless event processing** | Default for every Automated Order on first event | `ExceptionRecord` only (no `OrderCase`); `parent_case_id = NULL` | None — the existing deterministic graph runs end-to-end | <$0.001 / event | <500ms |
| **T2 — Lightweight stateful case** | (a) Manual Order at email arrival, OR (b) any event reaching `MANUAL_REVIEW_REQUIRED` / `BLOCKED` / `FAIL_TO_HUMAN`, OR (c) any subsequent event for an existing case | `OrderCase` + `ExceptionRecord` children + episodic events | Yes — Case Agent runs | <$0.05 / event | <8s |
| **T3 — Long-lived compacted case** | Case open >7 days OR >25 episodic events OR working-memory load >8k tokens (whichever fires first) | T2 + compacted summary; older episodic events retained verbatim in DB but not eagerly loaded | Yes — Case Agent runs with compacted working memory | <$0.08 / event (compaction amortised) | <12s |

**Tier transitions are forward-only.** A case never demotes from T2→T1 (cases don't un-open) and never demotes from T3→T2 (compacted summary stays). This keeps audit narrative coherent.

### 7.2 The first-non-clean-event materialisation rule (binding)

For Automated Orders, the case is materialised **lazily**:

```
Event arrives → existing T1 graph runs → terminal status reached
  if status == COMPLETE:
    no case opened. ExceptionRecord (or success record) persists with
    parent_case_id = NULL. Total cost: ~$0.001.
  else:
    open OrderCase, set parent_case_id on this record, transition
    to T2. Future events for the same correlation key attach to the
    case. Total cost on this event: ~$0.001 (graph) + ~$0.005 (case
    open + first agent context build).
```

For Manual Orders, the case is materialised **eagerly** at email arrival because:
* The email itself is the substrate the agent reasons over from event #1; deferring case creation forces re-extraction of the same email if/when an issue arises.
* Manual Orders by definition involve multiple touchpoints (initial email, clarifications, follow-ups). Statelessness doesn't fit the workload shape.
* SLA tracking on Manual Orders starts at email-receive time per business policy; the case must exist to hold that clock.

### 7.3 The memory hierarchy (MemGPT-style, applied to our domain)

| Tier | What it holds | Where it lives | Loaded into agent context? | Eviction policy |
|---|---|---|---|---|
| **Working memory** | Current event + 1-paragraph case summary + last 5 case actions + active L0 skill anchor + tool-call manifest | In-context, current turn | Always | Lives one turn; rebuilt next turn |
| **Episodic memory** | All prior case events / actions in detail (every tool call, every result, every state transition) | Postgres `case_events` table (append-only) | On-demand via `read_case_events` tool | Retained verbatim; compacted summaries supplement (do not replace) |
| **Semantic memory** | Customer profile, contract terms, account history, customer-tier matrices | Vector DB / structured retrieval (existing patterns: `entity_profile`, `impact_metrics`) | On-demand | Cache by tenant; refresh on customer-master change |
| **Procedural memory** | Recipes (L1), L0 skill bundles, policy tables, audit-bearing registry | Code + L0 directory | Loaded by L4 at startup (recipes); L0 loaded per-skill on case open | Code release / bundle deploy |

**Working memory is the bottleneck and the design surface.** Everything in working memory pays input-token cost on every inference. Everything *not* in working memory is retrievable but invisible to the agent unless it asks. The compaction protocol (§7.4) is the mechanism that keeps working memory bounded as cases age.

### 7.4 The compaction protocol (Compliance ratification required)

**Trigger:** working-memory load exceeds 8k tokens, OR 25 episodic events accumulated, OR case open >7 days, whichever fires first. The harness emits a `compact_case_requested` system event; the next agent turn observes it and either calls `request_compaction()` or proceeds (the agent has a small say in timing — it can ask to finish a tool sequence before compaction fires).

**Compaction process (deterministic L4 step, not L3 LLM-driven):**

1. Read all episodic events since the last compaction.
2. Apply a **deterministic summarisation template** per event-type:
   * Tool calls → `(tool_name, key_params, result_status, key_output_fields)` reduced form
   * Recipe results → classification + recommended_action + key audit-bearing fields
   * Human actions → who, when, action, reason
   * Buyer communications → drafted/sent flag, template name, fields-substituted
3. Concatenate summaries; compress with stable structure; cap at ~2k tokens.
4. **Persist the compaction** as its own audit-log event: `(compaction_id, events_summarised, summary_text, harness_version, timestamp)`.
5. Update `OrderCase.working_memory_summary` to point at the compaction.
6. **Original episodic events are retained verbatim in `case_events` table** — never deleted. Compaction affects context-load, not persistence.

**Why deterministic compaction (not LLM-driven):**
* Replayability: the compacted summary is reproducible bit-for-bit given the same inputs and template version.
* Audit defensibility: Compliance can challenge a decision and replay against the compaction *and* the underlying events. There's no LLM rewrite to dispute.
* Cost: deterministic templates are free vs. paying for an LLM compaction call per case.
* Quality: structured summaries from typed event records are *more* accurate than LLM summaries for our domain (tool calls are already structured; we don't need the LLM to extract structure from prose).

The L0 layer carries the compaction templates: `knowledge/compaction/<event_type>.template.md`. Compliance has CODEOWNERS gate on these templates because the summary IS what the agent sees post-compaction; if the template loses an audit-bearing detail, the agent's next decision degrades.

### 7.5 Compliance ratification of compaction

The user explicitly raised this — Compliance gets veto on compaction because compaction affects what evidence the agent has at decision time. The ADR commits to:

1. **Compaction templates are CODEOWNERS-gated** by Compliance + domain SME.
2. **Compaction events are themselves audit-log entries** with their own version stamp and reproducibility guarantee.
3. **Original events are retained in the database forever** (subject to data-retention policy applicable to all audit data). Compaction changes context-load only, not persistence.
4. **Audit query can replay** any past decision against (a) the compacted summary the agent saw at the time, AND (b) the underlying events. Both views are defensible; compaction-replay-divergence is itself an SLI.
5. **Compaction-template version** is in every audit-log entry from the moment compaction first fires on a case.

### 7.6 Why MemGPT-style and not "load entire case history"

Brute-force "load entire case" works for the first ~5 events and then falls apart on three counts:

* **Cost:** at 25 events × ~500 tokens each = 12.5k tokens of context just for history, on every inference. Doubles when working memory has ~12k of other content. Per-case agent run cost goes from $0.05 → $0.30+ for long-running cases.
* **Cache:** every new event invalidates the prefix because history changes. Cache hit rate collapses. Per-inference cost on already-paid-once content goes from cache-rate to full input-rate.
* **Reasoning quality:** "lost in the middle" is well-documented for transformer attention — material in the middle of a long context is reasoned about less reliably than material at the start or end. A 25k-token history with the current event at the end means the agent's signal-to-noise on what matters today degrades.

Compaction + selective retrieval gives:
* Bounded context (8k working memory cap)
* Stable cached prefix (compaction summary changes rarely)
* Concentrated signal (current event + last 5 actions + summary, not 25 events)
* Audit-defensible because the underlying events are still persistent

### 7.7 Backfill of existing flat exceptions (operational policy)

When this ADR ships:

* **Existing `ExceptionRecord` rows** keep `parent_case_id = NULL` initially. The system tolerates orphans on the existing graph path (T1).
* A **batch migration job** (`db/migrations/V010__backfill_order_cases.sql` + Python runner) auto-generates an orphan `OrderCase` per existing record so the data model becomes uniform, with `tier=1` (read-only historical), `source` inferred from `event_type`, `source_channel` set to a "legacy_pre_v10" sentinel where unknown.
* **Optional second pass:** records sharing `(tenant, customer_id, customer_po)` get merged onto a single case retroactively. This is best-effort and can be deferred indefinitely; the API tolerates one-record-per-case orphans without data-quality issues.

Existing UI tests + e2e assertions on flat exceptions continue to pass — the agent doesn't run on T1 historical records.

---

## 8. Cost & Latency Budgets, Governance

### 8.1 Per-tier budget table (binding for the L4 harness enforcer)

| Tier | Token budget (input/output per inference) | LLM-call budget per event | Wall-clock per event | $ per event (Sonnet pricing) | What happens at exhaustion |
|---|---|---|---|---|---|
| **T1** | 4k / 1k (intent classifier only; existing) | 1 (intent classify) | <500ms | <$0.001 | Existing graph behaviour; no agent loop |
| **T2** | 16k / 4k input/output growing per turn; up to 6 agent iterations | 3–6 LLM calls (1 classify + 2–5 agent iterations) | <8s | <$0.05 | Harness escalates: case → `OPEN_AWAITING_HUMAN`, reason `budget_exhausted` |
| **T3** | 8k / 2k (post-compaction; smaller because compacted summary replaces verbatim history) + retrieval as needed | 4–8 LLM calls per event | <12s | <$0.08 (compaction cost amortised) | Same escalation path as T2 |

The L4 harness enforces these per tier. The agent operates as if budget is infinite; the harness preempts and escalates when exhaustion is imminent.

### 8.2 Cost decomposition for a representative T2 case

Representative case: Manual Order email arrives, classifier runs, agent invokes `extract_attachment` (multimodal, cache miss), runs three validation tools, decides STANDARD_REVIEW with REQUEST_CLARIFICATION, drafts a clarification email.

| Component | Cost | Notes |
|---|---|---|
| Intent classify (existing) | $0.003 | 4k/200 tokens, Haiku-class |
| Case open + correlation lookup | $0.0001 | 1 DB roundtrip |
| Agent turn 1: read context, plan extraction | $0.015 | 11k input (cached prefix ≈ 3k after first call) / 800 output, Sonnet |
| `extract_attachment` (multimodal, cache miss) | $0.020 | Vision call on a 3-page PDF |
| Agent turn 2: read extraction, plan validations | $0.012 | Mostly cached prefix; 4k incremental input |
| `check_credit` + `check_atp` + `check_duplicate_po` | $0.001 | Three deterministic recipes |
| Agent turn 3: classify, draft clarification | $0.012 | |
| `draft_buyer_email` (server-render, no LLM body composition) | $0.000 | |
| Persist case events, update SLA clock | $0.0001 | DB writes |
| **Total per T2 first-event run** | **≈ $0.063** | |

The $0.063 is above the $0.05 target — the multimodal extraction is the pressure point. **Mitigations** (in order of effort vs gain):

1. **Template-fingerprint cache** for repeat customers' templates (Marcus Reed sends 30 POs/quarter on the same layout) — cuts attachment cost to $0 on cache hits. Expected hit rate after 90 days: 70%+. Effective per-T2 cost: ~$0.045. ✅ within target.
2. **Use Haiku-class (or local) for the agent loop** instead of Sonnet for routine cases; reserve Sonnet for cases the small model declines on. ~3× cost reduction on agent inferences. Stretch target: $0.025.
3. **Compose validation tools into one composite check** when the agent calls all three together. Saves agent-loop iterations. Marginal.

The point: **the budget is achievable without compromising the architecture; the dominant cost is multimodal extraction, and cache discipline is the lever.**

### 8.3 Latency decomposition for the same case

| Component | Latency | Notes |
|---|---|---|
| Intent classify | 200ms | Haiku-class |
| Case open + lookup | 20ms | |
| Agent turn 1 + extract | 2.8s | LLM 1.0s + multimodal 1.5s + IO 0.3s |
| Agent turn 2 + 3 validation calls | 1.4s | LLM 0.8s + 3 deterministic ~0.2s + IO |
| Agent turn 3 + draft | 0.9s | |
| Persist + SLA update | 50ms | |
| **Total** | **≈ 5.4s** | within 8s target |

Tail-latency mitigation: the harness has a 8s wall-clock cap per event; on breach, it returns the agent's most-recent decision-or-pending-state to the user and continues processing in the background. The case lifecycle marker `OPEN_AGENT_PROCESSING` flips to `OPEN_AWAITING_BUYER` / etc. asynchronously.

### 8.4 SLI / SLO discipline (operational governance)

| Indicator | Target | Action on breach |
|---|---|---|
| **T2 case agent run cost (p95)** | <$0.05 | If >$0.10 sustained, throttle agent iterations; investigate cost outliers |
| **T2 case agent run latency (p95)** | <8s | If >12s sustained, investigate; consider tool-call parallelism |
| **L0 skill cached-prefix hit rate (per skill)** | ≥70% | If <70% sustained, investigate prefix invalidation; likely an anchor-example edit in a hot loop |
| **Agent loop budget exhaustion rate** | <2% of T2 events | If >5%, the budget or the workflow is wrong; review cases that exhausted |
| **Compaction-replay divergence** | 0% | Any divergence is a code-correctness bug; halt rollout and fix |
| **Case-open rate (T2 / T3 from T1)** | Stable | If T2 case rate climbs without traffic increase, classification-quality regression; investigate |

### 8.5 Governance — who owns what at L0

| Artefact | CODEOWNERS gate | Rationale |
|---|---|---|
| `knowledge/skills/<name>/SKILL.md` | Compliance + domain SME + Engineering | Reasoning guide IS policy that drives audited decisions |
| `knowledge/skills/<name>/examples/*` | Compliance (review) + Engineering (author) | Examples shape agent behaviour; Compliance reviews the patterns. Per-example A/B test required. |
| `knowledge/skills/<name>/assets/*.template.md` | **Compliance + Brand** + Engineering | Customer-facing prose; brand-voice + legal review required |
| `knowledge/skills/<name>/specs/*.md` | Engineering + domain SME | Reference docs; not runtime |
| `knowledge/skills/<name>/metadata.yaml` | Engineering + Compliance | Routing + token-budget governance |
| `knowledge/compaction/*.template.md` | **Compliance + Engineering** | Compaction templates affect what the agent sees post-compaction; Compliance has veto |
| `compliance/audit_bearing_registry.yaml` | Compliance (existing) | Unchanged from current governance |

The pattern is consistent: anything that affects what the agent says, what it remembers post-compaction, or what it sends to a customer requires Compliance + a domain expert.

---

## 9. Migration Phases (H.1 → H.7) and Code-Level Scope

This is the operational rollout plan. **None of the phases requires a big-bang rewrite.** Each phase is independently shippable; existing T1 traffic continues uninterrupted throughout.

### Phase H.1 — Knowledge layer foundation (~1 week)

**Goal:** establish `knowledge/` directory and bundle structure without breaking existing skill loading.

* Create `knowledge/skills/<name>/` for each existing SKILL.md. Move SKILL.md files unchanged.
* Add `metadata.yaml` to each bundle with empty `examples`, empty `assets`, empty `specs`, no `anchor_examples` (initial state — pure repackaging).
* Update `skills/loader.py` to read from `knowledge/skills/<name>/SKILL.md`. Keep backward-compatible fallback to `skills/<name>_SKILL.md` for one release.
* Add CI check: every entry in `metadata.yaml::anchor_examples` exists; `runtime_includes` allowlist is honoured by the loader.
* **Existing graph + tests run unchanged.** No agent code yet; no behavioural change.

### Phase H.2 — `OrderCase` primitive + correlation table (~1 week)

**Goal:** persistence primitive in place; T1 path unaffected.

* `contracts/models.py` += `OrderCase` Pydantic model.
* `db/migrations/V009__order_case.sql` + `V010__case_correlation_keys.sql`.
* `api/store.py` + `db/repository.py` += `OrderCase` CRUD.
* `ExceptionRecord.parent_case_id` added as nullable column.
* No behavioural change yet — every record still has `parent_case_id = NULL`.
* Tests: CRUD, correlation lookup-or-create, multi-PO email correctly opens N cases.

### Phase H.3 — Tier-2 case materialisation on existing flows (~2 weeks)

**Goal:** existing exceptions start opening cases on the non-clean path. **No agent yet.**

* Update `orchestration/nodes.py::build_analysis` to call `case_resolver.lookup_or_create(event)` when `final_status != COMPLETE`. Set `parent_case_id` on the record.
* `OrderCase.tier = 2` for all new cases; SLA clock starts; lifecycle status follows the existing exception lifecycle.
* Tests: every existing e2e test verifies `parent_case_id` is set on non-clean records and a case row exists.
* Backfill job (`db/migrations/V011__backfill_orphan_cases.sql`) — optional, can defer.
* **Behavioural change:** the UI gains a `case_id` field on every non-clean record. Existing detail pages can be reshaped to show case context (Phase H.6 below).

### Phase H.4 — L2 attachment-extractor primitive (~2 weeks)

**Goal:** the multimodal extraction tool the Case Agent will need.

* `agents/primitives/extract_attachment.py` — the tool wrapper.
* Internal pipeline: template fingerprint → cache lookup (per tenant) → format dispatch (native PDF / OCR / Excel / multimodal image) → structured output.
* Cache-key includes `tenant_id` (PO Q5 binding).
* CI fixtures: 5 representative document shapes per format; structured-output assertions.
* **No agent yet — this is just an L2 primitive that other code can call.**

### Phase H.5 — Case Agent (L3) + Harness extensions (L4) (~3-4 weeks)

**Goal:** the agent runs on T2 cases.

* `agents/case_agent.py` — the loop.
* `agents/case_tools.py` — the 18-tool surface.
* `agents/working_memory.py` — context builder honouring §5.3 cache-discipline order.
* `agents/budget.py` — per-tier budget enforcement.
* `agents/compaction.py` — deterministic compaction (used in T3, but plumbed in this phase).
* L4 harness extensions: case-aware concurrency lock; tool-call interception for replay log; tier graduation on first non-clean event for Automated.
* **Initially route only NEW Manual Order events** (currently zero in production until ADR-034 ships) through the agent. Existing exception flows continue on the deterministic graph; `parent_case_id` is set but the agent doesn't run.
* Tests: agent loop with mocked LLM; full e2e with stub LLM responses producing each terminal state.

### Phase H.6 — UI: case detail surface (~2 weeks)

**Goal:** the CSR's lived workflow surfaces correctly.

* Reshape the existing `ExceptionDetailPanel` into `CaseDetailPanel` with case header (source, source_channel, SLA, lifecycle status) + child records stacked below as section components.
* Existing `*Section.tsx` components (EmailSourceSection, EmailOrderEntrySection, MOQSection, …) mount on the case detail page via the existing data-presence pattern.
* List view: replace `/inbox` + `/exceptions` as separate primaries with a single `/cases` queue. SLA-driven sort. Filter chips per tier / source / customer.
* Both `/inbox` and `/exceptions` retain as **filtered views** of `/cases` for ADR-034 §6 compatibility, but the primary CSR surface is `/cases`.
* Migration of existing UI tests + lock tests.

### Phase H.7 — T3 compaction enable + SLA tracking + backfill (~2 weeks)

**Goal:** long-running cases are bounded; SLA tracking is real; legacy data is uniform.

* Compaction trigger fires; templates active; CI tests verify replay-divergence == 0.
* SLA clock policy table at `knowledge/policy/sla_per_customer_tier.yaml`; the harness reads at case open and stamps `sla_deadline`.
* Backfill job materialises orphan cases for legacy `ExceptionRecord` rows (one case per record initially; optional merge pass).
* Migration of existing four-eyes / cosign / override flows to operate on the case lifecycle (not the exception lifecycle in isolation).

### 9.1 Total scope and timeline (estimate)

* **~10–12 weeks of focused engineering** across Phases H.1 → H.7.
* Independently shippable; T1 traffic uninterrupted throughout.
* Risk concentration: **Phase H.5 is the load-bearing phase** (Case Agent + 18-tool surface + harness extensions). Spike H.5 first against a single skill (`email-order-entry`) before generalising.

### 9.2 Sequencing notes (architectural honesty)

* H.1, H.2 are pure plumbing — low risk, ship fast.
* H.3 introduces the case as a real entity but no agent — gives the UI team a concrete object to design against in parallel with H.5.
* H.4 (extraction) and H.5 (agent) can run in parallel; the agent depends on the extractor at runtime but H.5 can stub it during development.
* H.6 (UI) depends on H.3 (case persistence); can start design work earlier.
* H.7 closes the loop with compaction + SLA + backfill.

---

*Sections §10–§12 follow in subsequent commits.*
