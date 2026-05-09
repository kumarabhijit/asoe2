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

## 4. The Asymmetric Combination Rule (the load-bearing decision)

This is the single most important rule in the ADR. Compliance veto rests on it.

### 4.1 The combination truth table (binding)

For every event where the L2 LLM Shadow is invoked (gating in §6.2 below), L4's combiner produces the final verdict from the deterministic L1 verdict and the L2 LLM verdict per this table:

| L1 deterministic | L2 LLM | Final verdict | Lifecycle outcome | Rationale |
|---|---|---|---|---|
| **GREEN** | **AGREE** | **GREEN** | Proceed → recipe executes | Both gates clear; baseline auto-handle path |
| **GREEN** | **DISAGREE_DOWNGRADE** | **YELLOW** | `MANUAL_REVIEW_REQUIRED` | LLM caught a context the rules missed; human reviews. **The reason text from the LLM is surfaced verbatim in the audit log so the reviewer sees WHY it escalated.** |
| **GREEN** | **ABSTAIN** | **GREEN** | Proceed → recipe executes | LLM declined to opine; deterministic gate is sufficient. Logged as ABSTAIN for SLI tracking. |
| **YELLOW** | **AGREE** | **YELLOW** | `MANUAL_REVIEW_REQUIRED` | Both want review; review |
| **YELLOW** | **DISAGREE_DOWNGRADE** | **YELLOW** | `MANUAL_REVIEW_REQUIRED` | Already at YELLOW; LLM cannot DOWNGRADE further (DISAGREE_DOWNGRADE has no path further down to RED) |
| **YELLOW** | **ABSTAIN** | **YELLOW** | `MANUAL_REVIEW_REQUIRED` | Deterministic verdict stands |
| **RED** | *(L2 NEVER INVOKED)* | **RED** | `BLOCKED` | Deterministic-RED short-circuits. L2 LLM Shadow is **not even called** when L1 returns RED — saves cost and prevents any LLM observation of RED-flagged content. |

### 4.2 Why asymmetric (architectural reasoning)

**The structural property:** the L1 deterministic verdict is the **floor**. The L2 LLM verdict can only **raise** caution (push toward MANUAL_REVIEW_REQUIRED), never **reduce** it.

This is enforced in three places, defence-in-depth:

1. **In the schema (§3.2):** the `ShadowLLMVerdict.action` Literal omits any `DISAGREE_UPGRADE` variant. Constrained-output bounds the LLM to actions that don't violate the rule.
2. **In the combiner (§4.1):** the truth table never produces a final verdict more permissive than the deterministic verdict.
3. **In the gating (§6.2):** L2 is not even invoked when L1 returns RED. The LLM cannot "see" content the deterministic gate has already blocked, and cannot under any circumstances downgrade RED → YELLOW.

**Compliance keeps its veto:**

* RED stays RED. Any deterministic-RED record blocks the same way it does today; ADR-039 changes nothing for that path.
* GREEN can become YELLOW if the LLM catches something. This is a *more conservative* outcome than today, never a *less conservative* one. Errors of caution, not commission.
* GREEN cannot become RED via the LLM. RED requires deterministic policy violation; we don't let the LLM flip to RED on its own. (If we did, replayability would be at risk because LLM is less reproducible than rules.)

**Replayability is preserved:**

* Same deterministic verdict + same LLM input + same model + same temperature (0) + same cached prompt → same final verdict on replay.
* If a future audit query challenges the decision, the auditor can: (a) verify deterministic Shadow's output independently against the inputs, (b) replay the LLM call against the same model + prompt + inputs and observe ~the same output. Both reproductions are recorded in the audit log.
* The LLM call's verbatim input + output is persisted alongside the decision (see §7.2 audit-trail extensions).

### 4.3 What the rule does not cover (explicit gaps)

* **L2 LLM unavailable / timeout / rate-limited.** The harness falls through to deterministic-only verdict; logs a `shadow_llm_unavailable` event; the SLI tracks the rate. The case is NEVER blocked waiting on L2 Shadow availability — operational reliability outweighs the marginal safety net. See §5.4.
* **L2 LLM returns malformed output** (constrained-generation failure). Fall through to deterministic-only; log `shadow_llm_validation_error`; SLI tracks. Same rationale.
* **L2 LLM answer arrives late** (after the case has already been routed by deterministic verdict). The late answer is logged for SLI but does NOT retroactively change the verdict. The L4 harness has a strict wall-clock budget (§5.3).

### 4.4 The asymmetric property under multiple invocations

If the same record is reanalyzed (CSR triggers a re-run), each invocation's L1 + L2 verdicts are logged independently. The reanalysis history (existing `ReanalysisHistoryEntry` from ADR-027 / `reanalysis_history`) carries both verdicts per attempt. This means:

* An L1-GREEN / L2-AGREE on attempt 1 followed by L1-GREEN / L2-DOWNGRADE on attempt 2 is a legitimate audit narrative ("the agent reconsidered with newer context and the LLM caught something").
* The L4 harness logs each attempt verbatim; nothing is silently overwritten.

### 4.5 Escalation reason flow (UI-visible)

When the LLM downgrades GREEN → YELLOW, the operator reviewing the case must see *why*. The L4 harness:

1. Captures the LLM's `reason` field (one-sentence rationale, constrained-output) into the `ComplianceDecision.reasons` list.
2. Captures the LLM's `policy_concerns` (named concerns from the closed L0 vocabulary) into `ComplianceDecision.policy_hits`, prefixed with `LLM_SHADOW:` so they're distinguishable from L1 deterministic policy hits.
3. Surfaces both on the existing exception detail panel under "Compliance evaluation" — no UI change needed beyond a visual badge indicating the source (rule-based vs LLM-based).

The reviewer never sees the LLM's free-form reasoning unconstrained — they see the structured `reason` + named `policy_concerns`. Boris's "tools are the API contract" rule applied to UI rendering: the LLM speaks in vocabulary the audit log can validate.

---

## 5. Cost and Cache Discipline

### 5.1 What we are protecting against

Naive deployment of L2 LLM Shadow: invoke on every event. At ~$0.005 per inference (Haiku-class) × 100k events/quarter = $500/quarter. Tractable but adds 50ms-200ms latency per event, fragments the prompt cache, and provides marginal safety value on the >99% of events where deterministic Shadow's verdict is already correct.

The right framing: **L2 LLM Shadow is a high-leverage tool deployed selectively, not a default-on cost.**

### 5.2 The two gating triggers (when L2 Shadow is invoked)

L4's `shadow_audit` invokes the L2 LLM Shadow when **either** trigger fires:

**Trigger 1 — Financial-impact threshold.** When the proposed action's financial impact (`financial_impact_usd` per ADR-029 / `policy.py::HIGH_VALUE_OVERRIDE_THRESHOLD_USD`) is at or above a threshold, invoke L2 Shadow regardless of deterministic verdict. **Initial threshold: $500** (well below the four-eyes cosign threshold of $10,000, so we get LLM second-opinion coverage on a much wider band than just cosign-eligible cases).

**Trigger 2 — Deterministic YELLOW.** When deterministic Shadow returns YELLOW, invoke L2 Shadow regardless of financial impact. Rationale: YELLOW is already "the rules are unsure" — the LLM either confirms (audit-bearing reason captured) or DISAGREE_DOWNGRADE (still YELLOW; explicit reason captured). The marginal value is in *enriching the YELLOW reason*, not in changing the verdict.

**Composition:** the triggers are OR-combined. An event with `financial_impact_usd = $50k` AND deterministic-YELLOW invokes L2 Shadow once; the result applies to both trigger paths.

**What's NOT a trigger** (explicit non-invocation):

* Deterministic-RED — short-circuited per §4.1.
* Low-impact GREEN (`financial_impact_usd < $500` AND deterministic-GREEN) — the >99% case; deterministic gate is enough.
* Reanalysis where the prior attempt's L2 verdict is cached and inputs haven't changed (§5.5 below).

### 5.3 Wall-clock budget

The L2 Shadow inference has a strict **2-second wall-clock cap**. On breach, the harness:

1. Falls through to deterministic-only verdict.
2. Logs `shadow_llm_timeout` event with the elapsed time.
3. Counts the event toward the SLI alert budget (§7.3).
4. **Does not block the case lifecycle** — the case proceeds with deterministic verdict.

2 seconds is roughly the p99 of a Haiku-class inference on the ~3k-token input the L2 Shadow receives. Local Ollama models on commodity hardware are usually faster but inconsistent under load — the timeout protects against tail latency.

### 5.4 Failure handling (the L2 LLM is unavailable)

Failure modes the harness handles silently (without changing case lifecycle):

| Failure | Harness action | SLI impact |
|---|---|---|
| L2 model returns 5xx / connection error | Fall through to deterministic-only; log `shadow_llm_unavailable` | Counts toward `shadow_llm_unavailability_rate` SLI |
| L2 returns malformed output (constrained-generation rejected) | Fall through; log `shadow_llm_validation_error` | Counts toward `shadow_llm_validation_error_rate` SLI |
| L2 returns 4xx (quota / auth) | Fall through; log `shadow_llm_quota` | High-priority alert; quota config issue |
| L2 timeout (>2s) | Fall through; log `shadow_llm_timeout` | Counts toward `shadow_llm_timeout_rate` SLI |

**Operational invariant:** an L2 outage degrades the system to today's behaviour (deterministic-only Shadow), never below it. This is the lowest-risk failure-mode design.

### 5.5 Cache strategy

The L2 Shadow's input is `(intent, recipe_name, recipe_params_hash, proposed_action, deterministic_verdict, customer_profile_hash)`. Many shadow calls repeat — the same customer's same SKU triggers similar evaluations.

**Cache key:** SHA-256 of the JSON-canonical concatenation of the input fields above + the L0 shadow-LLM bundle version + the model id.

**Cache value:** the typed `ShadowLLMVerdict` plus a timestamp.

**TTL:** 24 hours. Long enough to soak up repeat traffic from the same customer's daily order patterns; short enough that L0 bundle updates and customer-profile changes propagate within a day.

**Tenant isolation:** cache key includes `tenant_id` per ADR-038 §5.8. A Tenant-A cache hit cannot serve a Tenant-B request even if the rest of the inputs are byte-identical.

**Effect on cost:** at 70% cache hit rate (sustained after 30 days of warm-up), per-trigger cost drops to ~$0.0015 effective. Combined with the gating in §5.2 (L2 Shadow runs on roughly 5% of events under conservative assumptions), the per-event amortised cost is roughly **$0.0001**.

### 5.6 Cost budget summary (binding)

| Tier (per ADR-038) | L2 Shadow invocation rate | Per-event amortised cost | Per-month cost @ 100k events |
|---|---|---|---|
| **T1 (clean automated)** | 0% (deterministic-GREEN low-impact; never triggered) | $0 | $0 |
| **T2 (stateful case, mid-impact)** | ~5% (financial-impact OR YELLOW path) | ~$0.0001 | ~$10 |
| **T3 (long-running)** | ~10% (more mid-impact decisions per case) | ~$0.0002 | ~$20 |

**Total monthly L2 Shadow cost at 100k events: ~$30.** Even at 10× that traffic the cost stays under the noise threshold of the existing classifier-LLM bill. The asymmetric value proposition is favourable.

### 5.7 What this discipline does NOT do

* **Doesn't cache across tenants** (already covered in §5.5; bears repeating because it's a frequent bug pattern).
* **Doesn't cache across L0 bundle versions** — when Compliance updates the shadow-LLM system prompt, the cache key changes, all entries invalidate. This is by design: a policy change must propagate immediately.
* **Doesn't cache across customer-profile changes** — the customer profile hash is in the cache key. A customer-tier change invalidates relevant cache entries.
* **Doesn't suppress observability** — even on cache hits the L4 harness logs the cache-hit event with the cached verdict for SLI tracking. We need to see when caching is/isn't earning its keep.

---

## 6. Phased Rollout (X.1 → X.4)

This ADR is **not enabled by code merge.** Each rollout phase requires its own ratification gate. Compliance signs off at each transition; below-the-line changes fail the gate.

### 6.1 Phase X.1 — Observe-only (4–6 weeks)

**Behavioural state:** L2 LLM Shadow runs on the gating-triggered subset of events. Its verdict is **logged but does not affect the final verdict.** The deterministic verdict drives lifecycle and routing exactly as today.

**What ships:**
* `compliance/shadow_llm.py` — primitive in place
* `knowledge/shadow_llm/` bundle — system prompt + concerns vocabulary + initial few-shot examples (Compliance-authored seed set)
* L4 harness `shadow_audit` — invokes L2 Shadow on triggered events, records verdict in `LLMCallTrace` + `ComplianceDecision.llm_shadow_verdict` (NEW field)
* Audit-trail extensions per §7.2
* SLI metrics emitted per §7.3
* Cache infrastructure (§5.5)

**What we measure:**
* Disagreement rate: how often L2 returns `DISAGREE_DOWNGRADE` against deterministic-GREEN.
* Confidence distribution: histogram of L2 confidence scores; expected median ~0.7; tails inform prompt engineering.
* ABSTAIN rate: how often L2 declines. High ABSTAIN rate means the prompt or examples need work.
* Cache hit rate (target ≥ 70% sustained after 30 days).
* Wall-clock distribution (target p99 ≤ 2s; tighter is better).
* Failure-mode rates (unavailability, timeout, validation_error).

**Exit criteria for X.2:**
* Disagreement rate is in a defensible range (5–15% of triggered events; outside that range is either too noisy or not earning its keep).
* False-DOWNGRADE rate (LLM disagreed but human reviewer subsequently approved the original action without override) ≤ 25%. This is the "is the LLM crying wolf?" check.
* Cache + cost SLIs at target.
* No `shadow_llm_validation_error` rate > 0.1% (constrained-output reliability).
* Compliance workshop reviews 30 days of disagreement traces and explicitly ratifies the combination rule (§4.1) for the next phase.

**X.1 risk if exit criteria miss:** stay in observe-only; iterate on prompt + examples; do not advance.

### 6.2 Phase X.2 — Enable downgrade for high-financial-impact (2–4 weeks)

**Behavioural state:** L2 LLM Shadow's verdict starts affecting final verdict, but **only for events where financial_impact_usd ≥ $10,000** (the existing four-eyes cosign threshold). Below that, L2 verdict is still observe-only.

**Why this gating choice:** the four-eyes threshold is the existing line where extra scrutiny is policy. Aligning L2 Shadow's first verdict-affecting deployment with that line means the worst case (a false DOWNGRADE) lands a $10k-impact decision in the cosign queue rather than auto-resolving it. This is a small reviewer-cost increase in the highest-stakes band.

**What ships:**
* L4 combiner enables the truth table in §4.1, gated on `financial_impact_usd ≥ HIGH_VALUE_OVERRIDE_THRESHOLD_USD`.
* UI: the existing exception-detail panel renders the LLM reason + policy_concerns when present (badge indicates "AI second-opinion downgrade").
* Reviewer training: CSR managers briefed on the new evidence row.

**What we measure (in addition to X.1 metrics):**
* Override rate on LLM-downgraded records: of cases L2 pushed from GREEN→YELLOW, what fraction does the human reviewer override back to APPROVED? High override = LLM is too conservative on this band.
* Reviewer time-to-disposition on LLM-downgraded vs deterministic-YELLOW records (should be similar; if LLM-downgraded takes notably longer, the reasons aren't actionable enough).

**Exit criteria for X.3:**
* Override rate on LLM-downgraded records ≤ 35% (most LLM downgrades land actionable evidence).
* Reviewer time-to-disposition delta ≤ 10% (LLM evidence is as easy to act on as rule evidence).
* Compliance workshop ratifies expansion to lower thresholds.

### 6.3 Phase X.3 — Enable downgrade across all financial impact tiers (4–6 weeks)

**Behavioural state:** L2 LLM Shadow's verdict affects final verdict for **all events meeting the §5.2 gating triggers** — `financial_impact_usd ≥ $500` OR deterministic-YELLOW. Below $500 deterministic-GREEN remains untouched (L2 not invoked).

**Why $500 as the lower bound for invocation:** the analysis behind the trigger choice — at this floor, daily traffic per CSR is bounded, the pure cost of L2 Shadow is dominated by the value of catching the rare miss, and review workload remains absorbable.

**What ships:**
* Combiner gating expands from `≥ $10,000` to `≥ $500`.
* No code change beyond the threshold constant.

**What we measure (continuing from X.2):**
* Aggregate override rate across all tiers (target ≤ 35%).
* CSR-team-level workload impact: review queue depth before/after; team capacity headroom.

**Exit criteria for X.4:**
* All X.2 / X.3 metrics within target bands sustained for 60 days.
* No reviewer-team capacity breach (queue depth p95 ≤ 1.5× pre-deployment baseline).
* Compliance workshop ratifies the phase-X.4 expansion.

### 6.4 Phase X.4 — Extend cross-check to deterministic-primary classifier (Failure-mode-A mitigation)

**Behavioural state:** Phases X.1–X.3 mitigated Failure-mode-B. X.4 widens cross-check coverage for Failure-mode-A.

Currently the `constraints/cross_check.py::cross_check` pattern fires only when an LLM-backed classifier is the **primary**. X.4 makes it fire **always**: deterministic and LLM classifiers run in parallel; on disagreement, route to `MANUAL_REVIEW_REQUIRED`. This is independent of the L2 LLM Shadow but the same architectural philosophy (LLM as second opinion with conservative-bias handling).

**Why X.4 is in this ADR:** symmetry. ADR-039 takes the position that the right answer to "LLM second opinion" is the **constraint of asymmetric authority + observe-first rollout**. The same shape applies at classify time.

**What ships:**
* `constraints/cross_check.py` — flag flip: cross-check fires regardless of primary backend.
* `orchestration/nodes.py::classify` — uses cross-check on every classification call.
* Same SLI / disagreement-tracking as X.1.

**What we measure:**
* Classify-time disagreement rate.
* Hidden-misclassification rate uncovered by extended cross-check (compare to baseline classifier-only).

**Why X.4 is the last phase:** widening cross-check has higher operational risk than narrowing it. Existing classifier-disagreement handling routes to `MANUAL_REVIEW_REQUIRED`; if disagreement rate spikes (e.g., model drift between deterministic + LLM versions), reviewer queue can flood. X.4 is gated on observed deterministic-classifier baseline drift rate from X.1–X.3 telemetry.

### 6.5 Rollback policy

At any phase, if Compliance or SRE flags an SLI breach, the rollout is rolled back **one phase**. There's no "rip out the LLM Shadow" emergency switch — that would be a code change. Phases are configuration:

```yaml
# knowledge/shadow_llm/metadata.yaml::rollout
current_phase: X.1                      # or X.2 / X.3 / X.4
financial_impact_threshold_usd: null    # null = observe-only; 10000 / 500 per phase
extended_cross_check_enabled: false     # X.4 = true
```

L4 harness reads `current_phase` at startup and on a SIGHUP. Rollback is a one-line config change + restart, not a code revert.

---

## 7. Audit-Trail Extensions, SLI Monitoring, Replayability

### 7.1 What changes in `ComplianceDecision`

The existing `ComplianceDecision` Pydantic model gains an optional second-opinion block:

```python
# contracts/models.py — additions
class ShadowLLMVerdict(BaseModel):
    """Recorded on every L2-invoked event, regardless of phase."""
    model_config = ConfigDict(extra="forbid")

    action: Literal["AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"]
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    policy_concerns: list[str] = []
    bundle_version: str                 # L0 shadow_llm bundle version at decision time
    model_id: str                       # provider-resolved model id
    request_id: Optional[str] = None    # provider request id for support tickets
    cache_hit: bool = False             # whether served from L4 cache (§5.5)
    latency_ms: int = 0
    cost_usd_estimate: float = 0.0


class ComplianceDecision(BaseModel):
    # existing fields ...
    llm_shadow_verdict: Optional[ShadowLLMVerdict] = None    # NEW
```

When L2 Shadow was not invoked (gating not triggered, OR L2 unavailable, OR phase X.1 deterministic-only), `llm_shadow_verdict` is None. When invoked, it's populated regardless of cache-hit status.

### 7.2 What goes into the audit log

Per audited decision, the audit-bearing record (existing `audit_bearing_registry.yaml::ComplianceDecisionData` plus extensions) carries:

* Deterministic verdict (status, reasons, policy_hits) — existing.
* `llm_shadow_verdict` block (when applicable) — NEW; full content above.
* The L4 combiner's resolved final verdict — derivable from the above; recorded explicitly for fast query.
* The combiner rule version — since the truth table itself is L4 code, the version is the harness version. Stamped on every record.

**Audit query "show me every case where the LLM Shadow downgraded":** SQL `WHERE llm_shadow_verdict.action = 'DISAGREE_DOWNGRADE'`. The structured field makes this a one-line query.

**Replay query "would today's deterministic Shadow alone have approved this case":** SQL `WHERE deterministic_verdict.status = 'GREEN'` — directly from the recorded deterministic verdict. ADR-039 doesn't break the existing replay path; it adds a parallel observation.

### 7.3 SLI dashboard (Prometheus)

| Metric | Target | Alarm threshold |
|---|---|---|
| `shadow_llm_invocation_rate` | Phase-dependent (X.1: ~5% of events; X.3: ~10%) | >20% sustained — gating triggers misconfigured |
| `shadow_llm_disagreement_rate` (DISAGREE_DOWNGRADE / total invocations) | 5–15% | <2% (LLM not earning keep) OR >25% (LLM too conservative) |
| `shadow_llm_abstain_rate` | <30% | >50% — prompt or examples insufficient |
| `shadow_llm_false_downgrade_rate` (LLM disagreed but human approved override) | ≤25% (X.1 gate); ≤35% sustained (X.2+) | >40% — LLM systematically wrong; halt rollout |
| `shadow_llm_cache_hit_rate` | ≥70% sustained | <50% — cache fragmentation; investigate |
| `shadow_llm_p99_latency_ms` | ≤2000 | >3000 — model latency regression |
| `shadow_llm_unavailability_rate` | <0.5% | >2% — provider issue; alert SRE |
| `shadow_llm_validation_error_rate` | <0.1% | >0.5% — constrained-generation regression |
| `shadow_llm_cost_per_event_usd` | ≤$0.0002 (amortised) | >$0.001 — cache or model regression |

Reviewer-side SLIs (added to existing reviewer telemetry):

| Metric | Target | Alarm threshold |
|---|---|---|
| `reviewer_override_rate_on_llm_downgrades` | ≤35% | >50% — LLM systematically wrong |
| `reviewer_time_to_disposition_llm_vs_rule_delta` | ≤10% | >25% — LLM evidence not actionable |
| `reviewer_queue_depth_p95` | ≤1.5× pre-deployment | >2× — reviewer-team capacity breach |

### 7.4 Replayability guarantee (the load-bearing audit property)

Given:
* The same case state (events, prior verdicts) at decision time.
* The same recipe params and proposed action.
* The same deterministic Shadow policy version.
* The same L0 shadow_llm bundle version.
* The same L2 LLM model id.
* Temperature 0 for L2 inference.

The L1 deterministic verdict reproduces bit-for-bit. The L2 LLM verdict reproduces with high probability (model-deterministic at temperature 0; small variance possible from provider-side inference-engine non-determinism — documented but bounded). The combined verdict is therefore reproducible to within the LLM's noise floor.

**Audit can therefore:**
* Replay against the recorded deterministic verdict and verify it matches today's deterministic Shadow output.
* Replay against the recorded LLM input + prompt + model + bundle and verify the output matches (modulo provider noise).
* Verify the combiner truth table produces the recorded final verdict.

If any reproduction fails, that's an observability bug or a model-drift event; either is itself an SLI to track.

---

## 8. Open Questions, Lineage, Definition of Done

### 8.1 Open questions

1. **L2 model choice.** Procurement track:
   * Anthropic Haiku (cheapest commercial; good constrained-generation reliability)
   * Local Ollama with 7B-class model (cost-zero per-call; quality variable; ops complexity)
   * Hybrid (local primary, Anthropic fallback)
   The decision is bounded by cost + reliability + the per-event $0.0002 amortised target. Owner: Tools Admin + Compliance.
2. **`knowledge/shadow_llm/concerns_vocabulary.yaml` content.** Initial 10-15 named concerns drawn from the override-reason vocabulary (ADR-033) plus net-new concerns Compliance identifies in the X.1 disagreement traces. Owner: Compliance + Engineering.
3. **The X.1 observe-only telemetry sample size.** Compliance wants to see disagreement traces; how many records constitute "enough"? Suggested ≥100 per disagreement category before X.2 ratification. Owner: Compliance.
4. **Few-shot example authorship.** First 5–10 examples in `knowledge/shadow_llm/few_shot_examples/`; should be drawn from real production overrides where today's deterministic Shadow gave GREEN but a CSR overrode within 7 days. Owner: Compliance + domain SME.
5. **Combiner-rule extensibility.** The truth table in §4.1 is small but might need extension if we add more LLM action types (e.g., `DISAGREE_DOWNGRADE_TO_RED` — explicitly NOT in this ADR but might come up). Decision: keep this ADR's table as-is; future actions require their own ADR. Owner: Architecture Review Board.
6. **Inter-tenant model contention.** With many tenants on a shared local LLM, queueing under load could push p99 latency past 2s. Need to model this against expected concurrent traffic. Owner: SRE.

### 8.2 Lineage

* **ADR-021..033, 037:** unchanged.
* **ADR-025 (gateway READS before shadow):** unchanged. The L2 LLM Shadow runs *after* gateway READS, just like the L1 deterministic Shadow does today. Same evidence is available to both.
* **ADR-027 (pipeline visualization, Proposed):** when shipped, the executed-trace gains an `llm_shadow` step alongside `shadow_audit`. The visualization shows both verdicts and the combiner result.
* **ADR-029 (override merge policy):** unchanged; the LLM Shadow's downgrade routes the same override paths.
* **ADR-032 (calibration deferral):** the LLM Shadow's audit log feeds calibration data — when CSR overrides an LLM downgrade, the (LLM_input, LLM_output, human_decision) triple is labeled training data for future model improvement. Calibration ADR-032 plumbing absorbs this without ADR-039 changes.
* **ADR-033 (override reason vocabulary):** the `concerns_vocabulary.yaml` for L2 Shadow leverages the same vocabulary discipline. New concerns are added through the same lifecycle.
* **ADR-038 (case-centric):** companion. ADR-039 is independently mergeable. If both ship, the L2 Shadow benefits from richer context (case summary + customer profile) per §3.4. If only ADR-039 ships, it operates on the existing event-level context.

### 8.3 Definition of Done for this ADR

ADR-039 is **Accepted** when:

* Reviewer chain has signed off: AI/Agentic Engineering Architect → Compliance Veto Holder → Tools Admin / SRE → Domain SME → Product Owner.
* Compliance has explicitly ratified §4.1 (combination rule) and §6 (phased rollout).
* Phase X.1 backlog is open; no implementation has started yet.
* ADR-038 has been reviewed in parallel (mergeable independently but cleanest to ratify together).

### 8.4 What this ADR does *not* commit to

* **Does not** replace the deterministic Shadow. (L1 stays as the floor.)
* **Does not** change the override / disposition / cosign flows. (L2 verdict feeds the same paths.)
* **Does not** expose the LLM's free-form reasoning to UI or audit. (Only the constrained `reason` + named `policy_concerns`.)
* **Does not** introduce subagents or multi-agent Shadow. (Single L2 inference.)
* **Does not** commit to a vendor for the L2 model. (Procurement track.)
* **Does not** handle Failure-mode-A on the LLM-primary classifier path (already addressed by existing cross-check pattern; X.4 widens to deterministic-primary).

---

*End of ADR-039.*
