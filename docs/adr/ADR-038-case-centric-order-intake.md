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

*Sections §5–§12 follow in subsequent commits.*
