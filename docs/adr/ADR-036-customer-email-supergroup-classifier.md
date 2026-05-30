# ADR-036: Customer-Origin Email Supergroup Classifier

**Status:** Accepted (2026-05-30) — Phases 1–3 built; live-model promotion (shadow→gate) pending calibration.
**Date:** 2026-05-30
**Deciders (proposed reviewer chain):** Principal AI/Agentic Engineering Architect; Domain Modeller / Taxonomy Steward; Compliance Engineer; Product Owner; CS Ops business lead.
**Applies to:**
* asoe2: `constraints/specs.py` (constrained-decision schema), `skills/` (new email-supergroup classifier + backends), `api/case_resolver.py::resolve_or_open_case`, `api/store.py::lookup_or_create` (classification-event write), `api/routes/sandbox.py` (producer stand-in), `db/seeds/case_taxonomy.yaml` (steward action — leaf decision), `contracts/_generated/taxonomy_constants.py` (regenerated), `compliance/audit_bearing_registry.yaml`.
* asoe-ui: `src/lib/mock-data/cases.ts` (the existing hand-authored `supergroupFor` switch this work supersedes on the real path), `src/app/cases/*` (Customer Inbox lens).

**Related:**
* ADR-034 (email-order-entry skill — extraction runs *upstream of the graph*; §6.2 channel-neutral event-type naming).
* ADR-038 (case-centric order intake — `OrderCase` parent + lazy materialisation; §3.2 channel-neutral issue naming).
* ADR-042 (customer-inbox prototype port — §5b/§6 explicitly defer the multi-category customer classifier to a separate email-intelligence-agent ADR; the inbox today is a hand-coded mock).
* `docs/specs/case-intent-supergroup-requirements.md` — the governed taxonomy spec (§5 routing-key, §6.3 CUSTOMER supergroups, §8.3 reclassification rights, §8.5 supergroup-is-not-routing, §8.6 classification history, acceptance #1).
* B1/B2 (PR #193) — supergroup-at-intake derivation for the **API** path (`contracts/taxonomy.py::supergroup_for_intent`).

---

## 1. Context

The governed taxonomy defines **12 CUSTOMER supergroups** (`case-intent-supergroup-requirements.md` §6.3): `SG_NEW_ORDER`, `SG_ORDER_CHANGE`, `SG_ORDER_STATUS_INQUIRY`, `SG_SHIPMENT_DISCREPANCY`, `SG_RETURN_RGA`, `SG_LOGISTICS_CHANGE`, `SG_BILLING_DISPUTE`, `SG_DOCUMENTATION`, `SG_COMPLAINT_SERVICE`, `SG_COMPLAINT_PRODUCT`, `SG_EDI_ESCALATION`, `SG_NEEDS_TRIAGE`.

**Only two are producible by backend code today** — `SG_NEW_ORDER` (via the `manual-order-intake` sandbox producer → `INT_MANUAL_ORDER_INTAKE` → `SG_NEW_ORDER`) and `SG_NEEDS_TRIAGE` (the unclassified sentinel). The other ten are **consumed/projected only** (`api/case_summary*.py`, the asoe-ui Customer Inbox), never **produced**.

The reason is structural. B1/B2 wired supergroup-at-intake for the **API path** by *deriving the supergroup from the classified leaf intent* (`supergroup_for_intent`, inverting `INTENTS_BY_SUPERGROUP`). But the ten customer supergroups have **empty leaf-intent lists** in the taxonomy by design (`taxonomy_constants.py` `INTENTS_BY_SUPERGROUP` — only the 8 API/block supergroups plus the two customer sentinels carry leaves). A derive-from-leaf mechanism therefore **structurally cannot** emit them.

This is not an oversight in B1/B2 — it reflects the spec, which gives the two origins **deliberately different** supergroup-assignment mechanisms:

> **Acceptance #1** (`requirements.md:348`): *"A case can be created via the CUSTOMER path with `supergroup_code` set **from intake classification**, and via the API path with `supergroup_code` **derived from the SAP block code** through `case_intent.sap_block_code`."*

The "intake classification" for the customer path is an upstream **email-intelligence-agent**, which is **designed but not yet built** and explicitly scoped to a separate ADR:

> ADR-042 §6 (Option-B rejection): the multi-category customer inbox is *"gated on an `email-intelligence-agent` integration ADR (today the inbox is a hand-coded mock; the upstream classifier is platform-track work)."*

The visible consequence: on Azure / real traffic the Customer Inbox can only ever show `SG_NEW_ORDER` intake cases, while the asoe-ui mock fabricates the full variety client-side from a hand-authored `event_type → supergroup` switch (`asoe-ui/src/lib/mock-data/cases.ts::supergroupFor`) that is self-labelled *"mock-fixture demo plumbing, not a taxonomy claim."* The preview and production diverge, and there is no backend path to close the gap.

This ADR specifies the customer-origin supergroup classifier so the gap can be closed honestly.

## 2. Decision

Adopt an **upstream, constrained-generation email-supergroup classifier** that classifies an inbound customer email **directly into one of the 12 CUSTOMER supergroups**, stamps it on the case at open with `classifier_type=MODEL` + confidence + `model_version`, and writes one `case_classification_history` row. This mirrors the existing intent-classification pattern (`skills/intent_classifier.py` + `constraints/specs.py::IntentDecision`) and honours the determinism / constrained-generation guardrails (CLAUDE.md §3).

Five sub-decisions, each resolving an open question from the design investigation:

### D1 — Classifier output target: the **supergroup directly**, not a leaf

The classifier emits a `SupergroupCode` constrained to `SUPERGROUPS_BY_ORIGIN["CUSTOMER"]` (the 12 codes). It does **not** emit a leaf intent that then derives a supergroup.

*Rationale.* The spec makes supergroup a **case-level rollup, never a routing input**:
> §5 (`:86`): *"Routing key: **leaf `intent_code`**, never `supergroup_code`. Super-group is reporting/rollup only."*
> §8.5 (`:264`): *"Super-group is **never** a routing input."*

§8.6 explicitly permits a **null parent-level `intent_code`** in `case_classification_history`. So a customer case may carry a supergroup with no leaf — the supergroup is reporting, the (later) leaf is routing. Outputting the supergroup directly avoids requiring the customer leaf taxonomy to be filled first (a Phase-0 steward action, see D2), and matches the spec's "from intake classification" mechanism rather than re-using the API path's derive-from-code mechanism.

### D2 — Pure-intake categories are **supergroup-only at intake**; leaves are a deferred steward action

Categories like `SG_ORDER_STATUS_INQUIRY`, `SG_COMPLAINT_SERVICE`, `SG_DOCUMENTATION` are non-actionable intake — there is no deterministic recipe to run. At intake the case carries the **supergroup only** (parent-level classification, `intent_code = NULL`), and sits in human review. The case does not auto-materialise a child/leaf for these categories.

Filling actionable leaf intents per customer supergroup (e.g. `INT_SHORT_SHIP` under `SG_SHIPMENT_DISCREPANCY`) is the **Phase-0 data-mining + steward action** the requirements already scope:
> §6.4 (`:148`): *"The final leaf list per super-group is produced by Phase 0; PO approves the additions in a 30-minute review."*

This ADR does **not** invent customer leaf intents — that is steward/PO territory (§9.1). It only adopts the supergroup-only-at-intake shape, which §8.6 already supports.

> ⚠️ **Invariant impact (needs ratification).** The current child-materialisation invariant (S15a, `case_resolver.should_materialise`) and the v1 leaf-validity trigger (`supergroup_intent_allowed`) assume a child/leaf. Supergroup-only customer cases require confirming that (a) a parent case may exist with zero children for non-actionable intake, and (b) the inheritance trigger is not violated by a null child. This is **decision D2 for the reviewer chain** — see §5.

### D3 — Event types stay **channel-neutral**; the mock's `EMAIL_*` names are **not** ported

The asoe-ui mock keys its switch off channel-specific event types (`EMAIL_ORDER_CHANGE_REQUEST`, `EMAIL_INQUIRY`, `EMAIL_COMPLAINT`, `EMAIL_GENERAL`). These must **not** become canonical backend event types: channel-specific event naming is an **explicitly deprecated pattern**.

> ADR-034 §6.2 (`:238-240`): *"ADR-038 §3.2 (channel-neutral issue naming) makes channel-specific event names a deprecated pattern. `EMAIL_ORDER_ENTRY_REQUEST` is channel-specific by construction"* — which is exactly why it was renamed to the channel-neutral `MANUAL_ORDER_INTAKE`.

The categorization is the **classifier's output** (the supergroup), not an event-type literal. The inbound event keeps a channel-neutral `event_type` and carries the email payload on `metadata` / `enrichment_context["email_source_context"]` (ADR-034 HALT #4: no new first-class `OrderEvent` field; extraction outputs ride on `event.metadata`). The mock's switch is superseded on the real path by the classifier; it remains valid as labelled demo plumbing until the UI consumes real classifications.

### D4 — Confidence threshold 0.85; low-confidence sink is `SG_NEEDS_TRIAGE`

The model writes `classifier_type=MODEL` only at confidence **≥ 0.85** (env-configurable), per §8.3 (`:242`) and R3 (`:383`). Below threshold the case opens as **`SG_NEEDS_TRIAGE`** — the honest "couldn't classify" sink, which carries the §8.2 forcing functions (hard-block at close, 48h auto-age, weekly Top-10, <3% per-CSR target). No provisional best-guess supergroup is stamped below threshold.

Calibration of that confidence (ECE) is a precondition before it gates anything (ADR-032 calibration deferral; `customer-inbox-tdd-strategy.md` #4). Until calibrated, the live classifier runs in **shadow/observe** and the deterministic shim (D5) drives behaviour.

### D5 — Bootstrap with a labelled **deterministic backend**, live `Outlines` backend behind it

Exactly as intent classification splits `DeterministicFallbackBackend` (tests/sandbox) vs `OutlinesConstrainedBackend` (live), the email-supergroup classifier ships with:
* a **deterministic backend** that maps the seeded sandbox producer's category hint → supergroup (matching the asoe-ui mock vocabulary, so tests + the seeder are deterministic and the preview and Azure finally agree), and
* a **constrained live backend** (Outlines/Guidance over `SUPERGROUPS_BY_ORIGIN["CUSTOMER"]`) that does real classification once the email sanitizer (DoR gate #1) extends to body/attachment text.

The deterministic backend is the honest, governed bootstrap — it is a *stand-in for the model*, named as such, not a permanent hand-authored taxonomy rule.

## 3. Architecture

```
inbound customer email
   │  (upstream email-intelligence-agent — this ADR; sandbox stand-in today)
   ▼
OrderEvent(event_type=<channel-neutral>, metadata={email_source_context,…})
   │
   ▼
classify_email_supergroup(state) ──► EmailSupergroupDecision{
        supergroup_code: Literal[CUSTOMER codes],   # constrained generation
        confidence: float,
        rationale: str | None }
   │  conf ≥ 0.85 → MODEL ;  conf < 0.85 → SG_NEEDS_TRIAGE
   ▼
resolve_or_open_case(...)  ── sets case.supergroup_code (parent-level)
   │                            classifier_type=MODEL, model_version, taxonomy_version
   ▼
case_classification_history  ◄── exactly one row (§8.6)
```

The classifier is **non-executing**: it classifies and routes, it does not run a recipe or a compliance shadow inside itself (CLAUDE.md Reasoning Boundaries). Recipes remain driven by the leaf `intent_code`; this classifier only fills the case-level supergroup.

### 3.1 Layering — where the email-intelligence engine lives (and why not `skills/`)

This component reasons (Phase 3 is an LLM backend + OCR backend + rule fallbacks over free-text email bodies and attachments), so the intuitive home is `skills/`. It is **deliberately not** there, and the distinction is worth stating because it will recur every time someone adds a classifier:

* `skills/` in this repo does **not** mean "anything that does AI reasoning." It means **reasoning whose output is the routing key** — `IntentClassifier` emits a leaf `intent` that *directly selects a recipe*. That is why `skills/` (with `recipes/` and `orchestration/`) is a **routing layer**, guarded by the routing-on-leaf invariant lock (`tests/test_routing_on_leaf_only.py`), which forbids the token `supergroup_code` anywhere in those three directories so nobody can dispatch a recipe on a supergroup (§8.5).
* The discriminator is **output semantics, not input sophistication.** The email classifier's output is the **supergroup** — a reporting/rollup axis that, by §8.5, *never* reaches recipe dispatch. So however sophisticated its internals become, it is **not** a routing skill. The API path's `supergroup_for_intent` and the persistence-side `record_classification` write supergroup from the `api/` layer for the same reason; this classifier is their sibling.
* **Phase 1 (this PR) lives in `api/email_supergroup_classifier.py`** — correct, because the shim is ~30 lines of intake plumbing that relays a producer hint. **Phase 3 should NOT pile an LLM/OCR/rules inference pipeline into `api/`** (otherwise routes + persistence + intake orchestration). The clean Phase-3 home is a **dedicated non-routing reasoning module** — e.g. `email_intelligence/` (LLM backend, OCR backend, rule fallbacks) — sibling in spirit to `skills/` but outside the three routing directories, so the lock keeps holding. `api/case_resolver.py` calls into it through the **same seam** it uses for the shim today (`EmailSupergroupClassifier(backend=…).classify(state)`); only the backend swaps. The decision that matters is "**not in the routing layer**," not "`api/` specifically."

New `EmailSupergroupDecision` schema (sibling to `IntentDecision`):

```python
class EmailSupergroupDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supergroup_code: AllowedCustomerSupergroup   # Literal of the 12 CUSTOMER codes
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: Optional[str] = None
```

`AllowedCustomerSupergroup` is a hand-maintained `Literal` in `constraints/specs.py` (the same pattern as `AllowedIntent`), **locked by test** against `SUPERGROUPS_BY_ORIGIN["CUSTOMER"]` so it cannot drift from the taxonomy SoT — the same drift-lock guarantee as the generated taxonomy constants, without coupling the schema module to the codegen step.

## 4. Consequences

**Positive**
* Closes the preview-vs-Azure divergence honestly: the real path produces the same customer supergroup variety the mock fabricates, from a governed classifier instead of a hand switch.
* Fits the existing constrained-generation + Skill–Shadow–Recipe architecture with no new architectural primitive.
* Audit-complete: every customer case carries a `case_classification_history` row with `classifier_type` + `model_version` + `taxonomy_version` (§8.6, acceptance #9).
* Channel-neutral naming preserved (D3) — no deprecated event-type pattern reintroduced.

**Negative / costs**
* Requires a steward/PO taxonomy decision (D2 leaves) before actionable customer leaves exist; until then customer cases are supergroup-only review items.
* Requires the email sanitizer to extend to body/attachment text before the live model ingests untrusted email content (DoR gate #1) — a security precondition.
* Calibration work (D4) before confidence gates behaviour.
* The deterministic bootstrap shim is explicitly interim and must be retired when the live agent is calibrated, or it becomes a hidden second source of truth.

## 5. Open decisions for the reviewer chain

| # | Decision | Recommendation | Owner |
|---|---|---|---|
| D2a | May a CUSTOMER parent case exist with **zero children** (supergroup-only intake for non-actionable categories)? | **Yes** — §8.6 supports null parent-level leaf; required for inquiries/complaints. | Domain Modeller + PO |
| D2b | When are customer **leaf intents** mined/seeded per supergroup? | Phase-0 data-mining sprint per §6.4; out of scope for this ADR. | Taxonomy Steward + PO |
| D4 | Confidence threshold (0.85 default) and below-threshold sink (`SG_NEEDS_TRIAGE`). | Adopt 0.85 env-configurable; `SG_NEEDS_TRIAGE` sink. | CS Ops + Compliance |
| D6 | `EMAIL_COMPLAINT` / `CHANGE_ANALYSIS` referenced as `intent` in `case_summary_verdict_gates.py` Rule 7 — promote to real leaves, map to supergroups, or keep projection-only? | Resolve alongside D2b leaf seeding. | Domain Modeller |
| Sec | Extend the LLM input sanitizer to email body + attachment text before the live backend is enabled. | Block live backend on this; deterministic shim unaffected. | Compliance + Platform |

## 6. Implementation phases

1. **✅ Done (PR #194) — bootstrap shim.** `EmailSupergroupDecision` schema + test-locked `AllowedCustomerSupergroup`; deterministic backend; `EmailSupergroupClassifier`; wire into `resolve_or_open_case` for CUSTOMER origin; the sandbox producer emits the category hint via its existing `metadata_extra` passthrough; classification-history write; tests + a constrained-output lock. (The classifier later moved from `api/` into `email_intelligence/` in Phase 3 — see below.)
2. **✅ Done — PO-ratified customer leaf intents.** 36 leaves across 11 CUSTOMER supergroups added to `case_taxonomy.yaml` (proposal: `docs/specs/customer-leaf-intent-proposal.md`, ratified 2026-05-30) and regenerated. All `phase_zero_pending`; reclassification/reporting leaves only — **no recipe, not in `AllowedIntent`** (D1), so the intent↔recipe parity contract is untouched. Hand-edited (the seed's documented workflow) rather than the `steward_change` CLI, whose `yaml.safe_dump` round-trip would have stripped the file's comments.
3. **✅ Done — live engine (shadow until calibrated).** Built the dedicated **`email_intelligence/`** module (§3.1 home): `classifier.py` (router-wired), `shadow.py` (observe harness), `calibration.py` (ECE instrumentation). Added an `email_supergroup` `LLMTask`: `DeterministicFallbackBackend.classify_email_supergroup` (the fall-closed net) + `RemoteLLMBackend.classify_email_supergroup` (live, with the email text fenced by the existing `sanitize_email_text_for_llm`) + an `OutlinesConstrainedBackend` delegate. Selection is via `constraints.router.get_constrained_backend("email_supergroup")`, so it **gracefully falls back to the deterministic shim** when no provider/key is configured (local / CI / Vercel preview) and uses a real endpoint when `ASOE_LLM_PROVIDER[_EMAIL_SUPERGROUP]` is set — honouring kill-switch / explain-mode / per-task disable. Per D4 the live model runs in **shadow/observe** (`ASOE_EMAIL_SUPERGROUP_SHADOW=1`, default) — recorded for ECE, deterministic shim gates — until calibration; set `=0` to promote it to the gate. **Remaining (not code): join human-confirmed outcomes to compute ECE, then flip shadow off; the email-body sanitizer is wired but its allowlist tuning + OCR-derived attachment text remain ops follow-ups.**

## 7. Provenance

Derived from the deep parity-review session (case-table RLS / CI-hardening / supergroup-at-intake, PR #193) and the follow-up "B3" investigation, which established that the customer supergroups are unproducible today and that the spec intends an upstream model classifier (acceptance #1) deferred by ADR-042 §6 to a dedicated ADR. This is that ADR.
