# ADR-039: LLM Compliance Shadow Second Opinion (Asymmetric Downgrade-Only Authority)

**Status:** Proposed
**Date:** 2026-05-06
**Deciders:** Principal AI/Agentic Engineering Architect; **Compliance Veto Holder (load-bearing for this ADR)**; Tools Admin / SRE; Domain SME (CSR workflow); Product Owner.
**Applies to:** `compliance/shadow.py` (existing; unchanged), `compliance/shadow_llm.py` (NEW), `orchestration/nodes.py::shadow_audit`, `constraints/cross_check.py` (extended pattern), `contracts/models.py` (`ComplianceDecision` + `LLMCallTrace` extensions), `compliance/audit_bearing_registry.yaml` (new entries).
**Related:** ADR-025 (gateway READS before shadow); ADR-028 (DuplicatePO storage shape); ADR-029 (override merge policy); ADR-032 (calibration deferral); ADR-033 (override reason vocabulary); **ADR-038 (case-centric order intake — companion to this ADR)**.
**Companion to:** ADR-038. The two are independently mergeable; this ADR has no dependency on the case-centric model.

---

## 1. Context

### 1.1 What today's Compliance Shadow guarantees

`compliance/shadow.py` is **fully deterministic**. Given the same `(intent, recipe, params, policy thresholds)`, it returns the same `{GREEN, YELLOW, RED}` verdict bit-for-bit. The constrained-generation backends use a deterministic fallback for the shadow verdict slot (`AllowedShadowStatus = Literal["GREEN", "YELLOW", "RED"]`). The audit-trail story is that:

* Every shadow decision is reproducible against the same inputs and the same policy version.
* Replayability lets auditors challenge any past decision and verify the system would do the same thing today.
* Compliance has explicit veto: shadow's RED is never overridden by anything downstream.

This determinism is what makes the system defensible to auditors. It is **non-negotiable** as a floor.

### 1.2 What the deterministic floor doesn't catch

Two failure modes recurring in production telemetry:

**Failure mode A — intent classification error masked by Shadow.**
The intent classifier (LLM-driven when a remote backend is active; deterministic-fallback otherwise) misclassifies. The wrong recipe is selected. Shadow's deterministic rules run against the wrong recipe's policy. Shadow stamps GREEN because the wrong recipe's checks all pass on the wrong inputs. The wrong action ships.

* **Today's mitigation:** the `constraints/cross_check.py::cross_check` pattern fires at classify time when an LLM-backed classifier is active — it runs the deterministic classifier in parallel, and on disagreement halts to `MANUAL_REVIEW_REQUIRED`. This catches Failure-mode-A *only when an LLM backend is the primary classifier*.
* **Gap:** when the deterministic classifier IS the primary (most production traffic today), there's no second opinion at classify time. A subtle deterministic-classifier mistake passes silently.

**Failure mode B — Shadow's deterministic rules are insufficient for context.**
The intent is correctly classified. The right recipe is selected. Shadow's rules give GREEN because the rules don't model some context-specific subtlety:

* A customer-specific commercial agreement that allows pricing variance Shadow's general rule rejects.
* A blanket-PO release that Shadow's duplicate-PO rule wrongly flags.
* An MOQ shortfall that Shadow auto-rounds when the customer has explicitly opted out of round-up.
* A back-order substitution that's allowed for some SKU families but Shadow's general rule doesn't know that.

These are cases where **rules are right in 99% of traffic but miss the 1% where context matters**. Auditing them as misses is hard because they look like normal GREEN decisions.

### 1.3 Why this matters now

* Production telemetry over the past two quarters shows ~0.3% of GREEN-shadowed records get overridden by a CSR within 7 days. This is the empirical baseline of "Shadow's rules said yes but a human noticed it shouldn't have." A small % of high traffic is still hundreds of cases per quarter.
* Compliance has flagged (in the workshop notes from 2026-04-22) that "the deterministic floor catches the things rules can encode; we should plan for the things rules can't."
* The user (PO) has explicitly raised the future plan to use a local / low-cost LLM for Shadow verdict to avoid intent misclassifications and to add judgment to rule-only Shadow.

### 1.4 What we are NOT solving with this ADR

To set expectations honestly:

* **Hallucination in customer-facing outputs** — covered by server-side asset rendering (ADR-038 §5.6), not by adding LLM-in-Shadow.
* **Intent classifier accuracy** — that's a model / training / backend choice, not a Shadow concern. Failure-mode-A is partially mitigated by extending cross-check coverage (see §6.4 below) but the root fix is classifier improvement.
* **Replacing the deterministic Shadow** — this ADR explicitly does NOT replace it. The deterministic gate stays as the load-bearing floor.

The honest framing of what LLM-in-Shadow buys us: **a judgment-tier safety net for Failure-mode-B (Shadow's rules are insufficient for context), and a partial mitigation for Failure-mode-A on the path where the deterministic classifier is primary.** Both bounded; neither replaces existing controls.

---

## 2. Decision (summary; sections that follow expand each point)

This ADR establishes three binding decisions:

1. **Add an L2 LLM Shadow as a constrained-output second opinion** that runs alongside the existing L1 deterministic Shadow on a defined subset of cases (financial-impact threshold OR YELLOW-deterministic-verdict trigger). The L2 Shadow returns a typed verdict from a closed vocabulary. See §4.

2. **Apply asymmetric combination authority:** the LLM Shadow can **DOWNGRADE** the final verdict (e.g., turn deterministic-GREEN into final-YELLOW), but never **UPGRADE** it (no LLM-RED becomes final-GREEN; deterministic-RED is non-negotiable). See §5.

3. **Phased rollout (X.1 → X.4)** with observe-only Phase X.1 first. Compliance ratifies the combination rule based on observed disagreement-rate telemetry from Phase X.1 before any verdict-affecting deployment. See §7.

The architectural property that makes this safe:

> *L2 LLM Shadow can introduce non-determinism in the YELLOW-direction only. The deterministic floor of `(deterministic-GREEN AND no-LLM-DOWNGRADE) → final-GREEN` and `(deterministic-RED) → final-RED` is preserved exactly as today. Auditors can replay against the deterministic verdict and observe what would have happened pre-ADR-039; the LLM Shadow's contribution is a logged, reversible additional gate.*

---

## 3. Architecture — How L2 LLM Shadow Plugs into the System

### 3.1 The deployment shape (vis-à-vis ADR-038's five layers)

| Layer (per ADR-038) | Existing Shadow | L2 LLM Shadow (this ADR) |
|---|---|---|
| **L0 — Knowledge** | `compliance/audit_bearing_registry.yaml` (existing); shadow rule definitions in code | NEW: `knowledge/shadow_llm/system_prompt.md` (the LLM's policy guidance); `knowledge/shadow_llm/few_shot_examples/` |
| **L1 — Deterministic primitives** | `compliance/shadow.py::ComplianceShadow.evaluate()` returns `ComplianceDecision{status, reasons, policy_hits}` (unchanged) | n/a |
| **L2 — Bounded LLM primitives** | n/a | NEW: `compliance/shadow_llm.py` — single LLM call with constrained output; returns `ShadowLLMVerdict{action, reason, confidence}` from `Literal["AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"]` |
| **L3 — Case Agent** | Case Agent's tools that emit actions go through Shadow internally; no change | Shadow Combiner runs at the L4 boundary; L3 sees the combined verdict |
| **L4 — Harness** | `orchestration/nodes.py::shadow_audit` calls deterministic Shadow today | EXTENDED: `shadow_audit` calls deterministic Shadow first, then conditionally calls L2 LLM Shadow per the gating rules in §6.2, then applies the combination rule from §5 |

This ADR is independent of ADR-038's case-centric model. The same L2 Shadow architecture works whether or not the case primitive exists; the integration point is at `shadow_audit` in the orchestration graph.

### 3.2 The L2 LLM Shadow primitive (the new code surface)

```python
# compliance/shadow_llm.py — conceptual sketch

class ShadowLLMVerdict(BaseModel):
    """Constrained-output verdict from the L2 LLM Shadow."""
    model_config = ConfigDict(extra="forbid")

    action: Literal["AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"]
    reason: str            # one-sentence rationale
    confidence: float = Field(ge=0.0, le=1.0)
    policy_concerns: list[str] = []   # named concerns from a constrained vocabulary
                                       # (drawn from L0::knowledge/shadow_llm/concerns_vocabulary.yaml)


class ShadowLLM:
    """L2 LLM Shadow second opinion. Runs alongside L1 deterministic Shadow.

    Invoked only when gating rules in §6.2 fire. Returns a typed verdict
    that the L4 harness combines with the deterministic verdict per §5.

    Conservative bias by prompt: 'When in doubt, ABSTAIN. When you see
    something the rules might have missed, DISAGREE_DOWNGRADE. Never
    AGREE on something you genuinely cannot evaluate.'
    """

    def evaluate(
        self,
        intent: Intent,
        recipe_name: str,
        recipe_params: dict,
        proposed_action: str,
        deterministic_verdict: ComplianceDecision,
        case_context_summary: Optional[str] = None,    # if ADR-038 case is open
    ) -> ShadowLLMVerdict:
        # Constrained-output LLM call:
        #   - System prompt from knowledge/shadow_llm/system_prompt.md
        #   - Few-shot examples from knowledge/shadow_llm/few_shot_examples/
        #   - Constrained-output schema = ShadowLLMVerdict
        #   - Temperature 0 for replayability
        #   - Model: SMALL (Haiku-class or local Ollama; see ADR §10.8 procurement)
        ...
```

**Key design decisions baked into the primitive:**

1. **Constrained output schema** — the LLM cannot return free-form prose for `action`. Only `AGREE` / `DISAGREE_DOWNGRADE` / `ABSTAIN`. This is the same constrained-generation discipline applied throughout L2 today.
2. **Conservative bias** — the prompt explicitly instructs `ABSTAIN` over `AGREE` when uncertain. False ABSTAINs are cheap; false AGREEs are dangerous (they let through deterministic-GREEN that should have been YELLOW).
3. **No `DISAGREE_UPGRADE` action** — the constrained output deliberately omits any way for the LLM to *upgrade* a verdict. The asymmetric authority (§5) is structural in the schema, not a post-hoc filter.
4. **Temperature 0** — replayability requirement. Temperature > 0 introduces irreproducibility that breaks audit replay.
5. **Small / local model preferred** — the L2 Shadow operates on bounded inputs (intent, recipe params, proposed action, ~500 tokens of context summary). A 7B-parameter local model is plausible; Haiku is comfortable; Sonnet is overkill for this task.

### 3.3 Where the LLM Shadow's prompt comes from (L0 knowledge layer)

```
knowledge/
  shadow_llm/
    system_prompt.md                  # the LLM's policy guidance
    concerns_vocabulary.yaml          # closed list of named policy concerns
    few_shot_examples/
      blanket_po_release.example.json     # context where rule misses, LLM catches
      customer_specific_concession.example.json
      moq_opt_out_customer.example.json
    metadata.yaml                     # bundle version, model id, temperature
```

The system prompt + examples are versioned as L0 artefacts. CODEOWNERS gate: **Compliance + Engineering**. Compliance has veto on every change to the system prompt or examples — these are policy material that drives a verdict capable of escalating cases.

### 3.4 What the L2 Shadow sees (input contract)

The L2 Shadow inference receives:

* **Intent** (from L0 vocabulary) — `DUPLICATE_PO` / `PRICE_HOLD_RELEASE` / etc.
* **Recipe name** (from L1 registry) — for context.
* **Recipe params** (validated before this point) — the structured inputs the recipe ran on.
* **Proposed action** — the recipe's `recommended_action`.
* **Deterministic verdict** — `(GREEN, [], [])` or `(YELLOW, [reasons], [policy_hits])`. RED never reaches L2 Shadow (deterministic-RED short-circuits per §5).
* **Case context summary** (when an ADR-038 case exists) — compacted summary of prior actions on this case, useful for "we already escalated this last week" context.
* **Customer profile** (from L0 semantic memory) — customer tier, account flags, contract refs.

What the L2 Shadow **does NOT see:**

* Free-form email body / attachment bytes — the upstream extraction has already produced structured fields; L2 Shadow operates on structured data.
* The LLM intent classifier's confidence / rationale — the classifier and shadow are separate decisions; cross-contamination distorts both.
* Other open cases on the same customer — out of scope; case-correlation is a separate concern.

### 3.5 What the L2 Shadow does NOT do

* **Does not replace deterministic Shadow.** L1 runs first; L2 is a second opinion.
* **Does not change recipe outputs.** L2 reads the recipe's proposed action; it doesn't propose alternatives.
* **Does not write to the case directly.** All decisions flow through L4's combiner; L2 just returns a verdict.
* **Does not invoke other tools.** It's a single inference call; no tool use; no recursion.
* **Does not run on every event.** Gating rules in §6.2 keep the cost bounded.

---

*Sections §4–§8 follow in subsequent commits.*
