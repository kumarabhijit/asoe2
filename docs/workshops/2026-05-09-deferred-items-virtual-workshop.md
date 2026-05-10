# Virtual Workshop — Deferred-Items Pre-Read

**Date:** 2026-05-09
**Type:** Asynchronous virtual workshop — each expert lens contributes a position per agenda item; the chair (PO / Compliance Veto Holder) reconciles into actionable decisions.
**Trigger:** Compliance offline-approved the ADR-038 / ADR-039 / ADR-040 code paths shipped in PRs #120, #121, #135. This pre-read enumerates what each expert lens is expected to say about the three remaining deferred-item categories so the live workshop becomes a reconciliation, not a discovery.
**Scope:** Three categories from `tasks.md` post-PR-#121:
  1. Compliance ratification gates (ADR-038 §7.4 / §8.5; ADR-039 §4.1 / §6).
  2. Override-reason-tag curation (Phase 5.1 / 5.2 / 5.3).
  3. Adapter follow-ups (`adapt_price`, `adapt_duplicate`, `adapt_order_comparison`, `adapt_back_order`).

This document is a **pre-read**, not the workshop minutes. The minutes (with binding decisions) land after the live session.

---

## Participants (virtual lenses)

| Persona | Authority |
|---|---|
| **Compliance Veto Holder** | Load-bearing on every gated decision; can block any item. |
| **Principal AI/Agentic Engineering Architect** | Consistency with the deterministic Skill-Shadow-Recipe architecture; CLAUDE.md guardrails. |
| **Tools Admin / SRE** | Operational surface — provider credentials, env-var ConfigMaps, runtime resource budgets. |
| **Domain SME (CSR workflow)** | Whether the proposed control behaves the way real operators expect. |
| **Product Owner (PO)** | Roadmap sequencing; revenue / customer-commitment implications. |
| **ML / Calibration lens** | Vocabulary & training-data fitness for downstream calibration (ADR-032). |
| **Data Engineering lens** | Gateway / persistence gaps that block the adapter follow-ups. |
| **Frontend Platform** | UI surface implications of every backend contract change. |

---

## Agenda

### Item 1 — Compliance ratification gates (4 sub-items)

#### 1A. ADR-038 §7.4 — Compaction protocol ratification

**What's actually pending:** Sign-off on the per-event-type compaction templates (8 shipped under `knowledge/compaction/` in `04bfd9d`) and the §7.4 binding triggers (8k tokens / 25 events / 7 days).

| Lens | Position | Concerns |
|---|---|---|
| **Compliance** | Likely-approve. The shipped templates name `audit_keys` per event type, the line shape `[<event_type>@<timestamp>] key=value, ...` is reproducible, and `tests/test_compaction_sla_backfill.py::TestPerEventTypeTemplates::test_replay_invariant_across_template_load` locks byte-for-byte determinism. | One ask: explicit Compliance review checklist for `audit_keys` per template (not just "the keys we picked are sensible"). Recommend annexing the checklist to the ADR. |
| **AI/Architect** | Approve. The default-fallback path (`__general__.template.md`) means a missing per-event-type template is graceful-degrade, not a hard failure. Forward-compatible. | None blocking. |
| **Tools Admin / SRE** | Approve. No new infrastructure; templates load from disk at process startup with module-level cache (no per-event I/O). | Capacity ask: confirm the 2k-token compaction output cap holds under burst (verified in `test_summary_capped_by_target_tokens`). |
| **Domain SME** | Approve with one ask: an `override.template.md` field for the override `reason_code` is mandatory (already shipped). | None blocking. |
| **PO** | Approve. | None. |
| **ML lens** | Conditional approve. The compacted view feeds the calibration data the ML team eventually consumes (ADR-032). The chosen `audit_keys` per template determine what calibration sees. | Wants a one-line per-template note: "what learning signals this template preserves" so the calibration backlog has provenance. |

**Anticipated workshop resolution:** ADR-038 §7.4 ratified. Compliance annex deliverable: 1-page checklist per template (templates already self-document; promote frontmatter narrative to formal annex). ML lens deliverable: add `learning_signals:` block to each template's frontmatter (additive; backwards-compat).

---

#### 1B. ADR-038 §8.5 — Governance / CODEOWNERS map

**What's actually pending:** The five-layer CODEOWNERS map (L0 / L1 / L2 / L3 / L4 → review chain). `architecture_v5.md` §1.3 documents the rationale; the actual `.github/CODEOWNERS` file does not yet enforce it.

| Lens | Position | Concerns |
|---|---|---|
| **Compliance** | Approve **conditional on** `.github/CODEOWNERS` shipping. Today's CODEOWNERS file gates compliance-bearing files (`compliance/audit_bearing_registry.yaml`, `compliance/`); ADR-038 §8.5 wants the per-layer map to be machine-enforced. | Without the file change, the §8.5 ratification is words-only. |
| **AI/Architect** | Agree with Compliance. The five-layer model is meaningless if a contributor can land an L2 LLM-primitive change without Compliance + Tools Admin sign-off. | None. |
| **SRE** | Approve. Workload-Identity / Key Vault wiring is L4-gated already. Adding the per-layer chain is additive. | Wants `.github/CODEOWNERS` review session before ratification (not a pre-read item; a workshop deliverable). |
| **Domain SME** | Defer (no direct CSR-workflow implication). | None. |
| **PO** | Approve. | None. |
| **Frontend Platform** | One ask: confirm the asoe-ui repo gets a parallel CODEOWNERS update (`src/types/exceptions.ts` mirrors `asoe2/contracts/models.py` — must land in lockstep per CLAUDE.md Guardrail #3). | None blocking. |

**Anticipated workshop resolution:** ADR-038 §8.5 ratified contingent on landing `.github/CODEOWNERS` (asoe2 + asoe-ui) before flip. Engineering deliverable, scoped follow-up.

---

#### 1C. ADR-039 §4.1 — L2 LLM Shadow combination rule

**What's actually pending:** Sign-off on the asymmetric-authority truth table (L2 can DOWNGRADE only; never UPGRADE). The `combine_verdicts` function (shipped in `5cc8408`) encodes it; 21 unit tests + 2 e2e tests lock it.

| Lens | Position | Concerns |
|---|---|---|
| **Compliance** | Likely-approve — this is the load-bearing rule for the entire ADR-039. **Strongest concern of the workshop.** | Three sub-asks: (a) the `DISAGREE_UPGRADE` enum omission must remain structural (verified — `ShadowLLMVerdict.action` Literal); (b) every L2 invocation must be replayable (verified — temperature 0; cache key includes `model_id` + `bundle_version`); (c) the `LLM_SHADOW:` reasons-prefix must be visible to the human reviewer in every UI surface where the verdict surfaces (PARTIAL — backend stamps it; UI surfacing per ADR-040 §3.3 / V5.1 reshape). |
| **AI/Architect** | Approve. The asymmetric authority is enforced in the schema, the combiner, and the gating (defence-in-depth). No way for L2 to flip RED → GREEN. | None. |
| **SRE** | Approve. The X.1 → X.2 flip is one ConfigMap edit (`knowledge/shadow_llm/metadata.yaml::rollout.financial_impact_threshold_usd: null → 10000`). Rollback is the same edit reversed. | Wants the SLI dashboard (ADR-039 §7.3) ready before the X.2 flip — `shadow_llm_disagreement_rate`, `shadow_llm_false_downgrade_rate`, `reviewer_override_rate_on_llm_downgrades`. The counters exist; the Prometheus scrape + Grafana panel are ops follow-up. |
| **Domain SME** | Conditional approve. Reviewers need training before their queue gets new LLM-driven YELLOW records. | Asks for a 1-week soak in observe-only X.1 with the Azure provider (`AZURE_OPENAI_SHADOW_DEPLOYMENT` set, threshold null) before the X.2 flip. |
| **PO** | Approve. The X.2 → X.3 → X.4 phased rollout matches the §6 phasing. | None. |
| **ML lens** | Approve. The disagreement traces feed into the anchor-example accrual (`scripts/earn_anchor_examples.py`); the X.2 flip widens the training-data flow. | None. |
| **Frontend Platform** | Conditional approve — the §4.5 reasons-surfacing requirement needs UI work. The backend stamps `LLM_SHADOW:` prefixes today; the UI rendering is currently inherited (it shows whatever's in `reasons` / `policy_hits`) but the visual badge distinguishing L1-rule vs L2-LLM source is V5.1. | Asks for a 2-week notice before the X.2 flip so the badge ships. |

**Anticipated workshop resolution:** ADR-039 §4.1 ratified contingent on:
  * 1-week observe-only X.1 soak with Azure provider live (Domain SME ask).
  * SLI dashboard panels live before X.2 flip (SRE ask).
  * UI L1-vs-L2 badge shipped before X.2 flip (Frontend ask).
The X.2 flip is a config edit; the gates above are operational, not engineering.

---

#### 1D. ADR-039 §6 — Phased rollout (X.1 → X.4)

**What's actually pending:** Sign-off on the X.1 → X.2 → X.3 → X.4 phasing schedule (4–6 weeks each, with exit criteria per phase per ADR-039 §6).

| Lens | Position | Concerns |
|---|---|---|
| **Compliance** | Approve. Each phase has explicit exit criteria + workshop ratification gate. | The §6.1 X.1 exit criteria say "Compliance workshop reviews 30 days of disagreement traces and explicitly ratifies the combination rule for the next phase." That review needs the anchor-example accrual artifact (`scripts/earn_anchor_examples.py`) — which shipped in `97175d4` so this gate is unblocked. |
| **AI/Architect** | Approve. | None. |
| **SRE** | Conditional approve. Each phase flip is one ConfigMap edit, but the rollback path needs a documented runbook. | Wants the runbook drafted before X.2: "On SLI breach (false_downgrade_rate >40%; queue_depth_p95 >2× baseline), set `financial_impact_threshold_usd: null` and restart the relevant pods." The runbook is an SRE deliverable, not engineering. |
| **Domain SME** | Approve, contingent on the 1-week X.1 soak (see 1C). | None. |
| **PO** | Approve. The 4–6 week phase length matches the OKR cadence. | None. |
| **ML lens** | Approve. The X.4 extension to deterministic-primary classifier cross-check is the symmetry the ADR §6.4 anticipated. | Wants confirmation that the `cross_check_disagreement` SLI counter (already on `LLMCallTrace`) is wired to a Grafana panel before X.4. |
| **Frontend Platform** | Approve. The reviewer queue impact is the `reviewer_queue_depth_p95` SLI; the existing Exception Queue UI doesn't need new surface for the phasing. | None. |

**Anticipated workshop resolution:** ADR-039 §6 ratified. Operational deliverables before each phase: rollback runbook (SRE); SLI panels live (SRE); training brief (Domain SME).

---

### Item 2 — Override-reason-tag curation (Phase 5.1 / 5.2 / 5.3)

**What's actually pending:**
* §5.1 — Product / Compliance review of historical `change_reason` notes per intent; propose 4–6 categories per intent.
* §5.2 — Replace `INTENT_REASON_TAGS = {i: _GLOBAL_REASON_TAGS for i in ...}` seeding in `constraints/specs.py` with the curated table; regenerate openapi; update `TestPerIntentReasonTag`.
* §5.3 — Audit existing rows (grandfather under the global set vs re-label).

| Lens | Position | Concerns |
|---|---|---|
| **Compliance** | Approve §5.3 grandfather path emphatically — re-labelling existing rows would invalidate the hash chain (`policy_audit_log` event-hash). Re-label is a non-starter. | The grandfather path is "purely a validator change" per the existing tasks.md note; that change must not also relax forward validation. |
| **AI/Architect** | Approve. The curated table is `Dict[Intent, list[str]]` data; the validator stays unchanged. | Asks: keep `other` in every intent's set (already in tasks.md note) — prevents workflow dead-ends. |
| **Tools Admin / SRE** | Approve. No infrastructure impact. | None. |
| **Domain SME** | **Load-bearing on §5.1.** This is the persona that knows the historical reasons. | Wants a structured review template per intent (sample 100 rows; bucket into themes; iterate; share with Compliance). Suggests one curation session per intent rather than a single mega-meeting. |
| **PO** | Approve. The narrowing improves audit reporting clarity. | Wants the curation done before EOY 2026 (no hard deadline). |
| **ML lens** | **Strongly motivated** — this is the §5.4 follow-up Phase 5 unblocks. Once curated `(intent, reason_tag)` tuples exist, the ML team can cluster meaningfully. | None blocking. |

**Anticipated workshop resolution:** Phase 5.1 / 5.2 / 5.3 stays Domain-SME-driven; the workshop does not unblock §5.1 alone (the historical-data review is genuinely the SME's deep-dive, not a workshop deliverable). Workshop output: agree on the §5.3 grandfather path (Compliance preference) and ratify the §5.1 review template + cadence (one session per intent).

---

### Item 3 — Adapter follow-ups (4 sub-items)

**What's actually pending:**
* `adapt_price` (PriceAdjustmentRecipe → price_analysis). Blocked on `price_analysis_gateway_gap` clause + 2026-06-21 deadline.
* `adapt_duplicate` (DuplicatePORecipe → duplicate_detection). Blocked on persisting `matched_po_details` on the record.
* `adapt_order_comparison` — synthesised from duplicate; lands after `adapt_duplicate`.
* `adapt_back_order` (BackOrderResolutionRecipe → backorder_analysis). Blocked on persisting warehouse snapshots.

| Lens | Position | Concerns |
|---|---|---|
| **Compliance** | Approve **conditional on each adapter respecting the audit-bearing registry's grandfather clauses with their stated deadlines**. The `price_analysis_gateway_gap` carries a 2026-06-21 deadline; if the adapter ships earlier than the gateway gap fix, the registry must be updated atomically. | Each adapter PR must be co-reviewed against `compliance/audit_bearing_registry.yaml` to ensure no audit-bearing field is dropped. |
| **AI/Architect** | Approve. The adapter pattern is well-established (`api/analysis_adapters.py` already carries `adapt_price_hold` + `adapt_email_order_entry` + others). | Asks the Verdict Pillar 2 boundary be respected: recipes return dicts; `api/analysis_composer.py` projects them. No composition logic in the adapters or section components (CLAUDE.md §6). |
| **SRE** | Defer (no direct ops impact). | None. |
| **Domain SME** | Approve. These adapters unblock the Layer-2 evidence the CSR consumes for high-stakes decisions (price overrides; duplicate-PO resolutions). | Asks for early-preview of each adapter's UI rendering before merge so reviewers can validate the evidence layout matches CSR mental model. |
| **PO** | Approve. These adapters close the audit-bearing-registry coverage gaps. | The 2026-06-21 deadline on `price_analysis_gateway_gap` is binding — slippage past that date breaches the SOX commitment Compliance signed off on. |
| **ML lens** | Approve. Persisted `matched_po_details` (adapter 2) and warehouse snapshots (adapter 4) are direct training-data assets. | None. |
| **Data Engineering** | **Load-bearing on the gateway / persistence gaps.** | Status report: (a) `price_analysis_gateway` capability is mid-build by Procurement integration team; ETA 2026-05-25 — leaves ~4 weeks of adapter-build time before the 2026-06-21 deadline. (b) `matched_po_details` persistence is a 1-day SQL change (V014); planned for w/c 2026-05-12. (c) Warehouse-snapshot persistence requires a new gateway (`oms/get_warehouse_snapshots`); not yet scoped — will need a separate ADR. |
| **Frontend Platform** | Approve. The matching `*Section.tsx` components (`PriceHoldSection`, `OrderComparisonSection` etc.) already exist; adapters feed them via the data-presence dispatch (CLAUDE.md §6). | Wants 2-week heads-up before each adapter merges so the section component is reviewed for new fields. |

**Anticipated workshop resolution:**
  * `adapt_price` — Engineering builds in parallel with Procurement gateway; co-merge in late May / early June. Hard 2026-06-21 deadline.
  * `adapt_duplicate` — Data Engineering ships V014 w/c 2026-05-12; adapter immediately after.
  * `adapt_order_comparison` — sequence after `adapt_duplicate` ; targets w/c 2026-05-19.
  * `adapt_back_order` — out of scope for this workshop; needs its own ADR for the warehouse-snapshot gateway.

---

## Cross-cutting workshop asks

* **CODEOWNERS update** (asoe2 + asoe-ui) — ratification dependency for §8.5. Engineering deliverable.
* **SLI dashboard** (Prometheus scrape + Grafana panels) for the ADR-039 §7.3 surface — ratification dependency for X.2 flip. SRE deliverable.
* **Rollback runbook** for X.2 / X.3 flip — SRE deliverable.
* **Reviewer training brief** for L2-Shadow-driven YELLOW records — Domain SME deliverable.
* **UI L1-vs-L2 badge** distinguishing rule-based from LLM-based reasons in the Compliance evaluation block — Frontend Platform deliverable, V5.1.
* **`learning_signals:` frontmatter block** on each compaction template — additive; co-authored by ML + Engineering.

---

## Recommended decision matrix (chair's pre-read summary)

| Item | Engineering ready? | Operational deliverables outstanding | Recommended workshop verdict |
|---|---|---|---|
| 1A — ADR-038 §7.4 compaction protocol | ✅ Yes | None blocking | **Ratify** |
| 1B — ADR-038 §8.5 CODEOWNERS map | ✅ Yes (doc) | `.github/CODEOWNERS` update needed | **Ratify pending the file ship** |
| 1C — ADR-039 §4.1 combination rule | ✅ Yes | 1-week soak; SLI dashboard; UI badge | **Ratify pending operational deliverables** |
| 1D — ADR-039 §6 phased rollout | ✅ Yes | Rollback runbook; reviewer training; SLI panels | **Ratify** |
| 2 — Override-reason-tag curation | Partially (mechanism ready; curation pending) | Domain SME review template + cadence | **Set cadence; defer §5.1 to per-intent sessions** |
| 3 — Adapter follow-ups | Conditional (gateway gaps) | Per-adapter co-review w/ audit registry | **Sequence per Data Engineering ETAs; hard 2026-06-21 on `adapt_price`** |

---

*This pre-read is a Claude-authored simulation of expected workshop positions, not the workshop minutes. The minutes (with binding decisions, deliverable owners, and dates) land after the live session.*
