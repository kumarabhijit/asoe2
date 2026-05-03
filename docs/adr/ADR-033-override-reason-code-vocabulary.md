# ADR-033: Override Reason-Code Vocabulary Lifecycle (Per-Intent Curation)

**Status:** Accepted
**Date:** 2026-05-03
**Deciders:** Same as ADR-028 (review session 2026-05-03). Product + ML jointly owned per the meeting action item.
**Applies to:** `constraints/specs.py` (`INTENT_REASON_TAGS`, `AllowedOverrideReasonTag`), `api/routes/health.py` (`/health.allowed_override_reason_tags_by_intent`), `asoe-ui/src/app/exceptions/OverrideChooserDialog.tsx`, `tests/test_constraints.py`.
**Related:** ADR-028, ADR-029, ADR-030, ADR-032.

---

## Context

`constraints/specs.py` already exposes a global `AllowedOverrideReasonTag` Literal and an `INTENT_REASON_TAGS` map keyed by intent. The map's current value is intentionally placeholder — every intent points at the same global set:

```python
_GLOBAL_REASON_TAGS: tuple[str, ...] = (
    "customer_concession", "contract_stale", "data_error",
    "policy_exception", "agent_misclassification", "other",
)
INTENT_REASON_TAGS: dict[str, tuple[str, ...]] = {
    intent: _GLOBAL_REASON_TAGS for intent in AllowedIntent.__args__
}
```

The existing comment block in `constraints/specs.py` calls this out as a Phase-3 follow-up waiting on curated vocabulary from product + compliance. The new duplicate-PO spec — specifically `docs/specs/duplicate-po/calibration-methodology.md` — now hands us that curated vocabulary for `DUPLICATE_PO`:

```
INTENTIONAL_REORDER     -- customer genuinely placed a second order
AMENDED_PO              -- this is a revised version, not a duplicate
BLANKET_RELEASE         -- release against a blanket PO
SYSTEM_RETRY_VALID      -- middleware retry was intentional/valid
DIFFERENT_SHIP_TO       -- different destination = different order
CONFIRMED_DUPLICATE     -- agent was correct
PARTIAL_OVERLAP         -- some lines overlap but order is distinct
OTHER                   -- free-text required
```

ADR-032 establishes that calibration is deferred but customer-supplied. The labeled training data calibration will eventually need is `(intent, recommended_action, chosen_action, reason_tag)` tuples — exactly what these reason codes produce. Curating `DUPLICATE_PO` reason tags now is therefore both a UI improvement (better override workflow) and a data-collection investment for future calibration.

End-user (CS associate U1) feedback in the design review:
- Eight options on a hot queue is too many; will default to `OTHER` if the modal is too dense.
- Visual grouping into 3 clusters (agent-was-wrong / agent-was-right-but-business-decision / edge-case) is more usable than a flat 8-item list.
- Free-text alongside the structured code remains valuable for audit narrative; should be optional except for `OTHER`.

This ADR establishes the lifecycle for per-intent reason-code vocabularies so future intents can land their own curated sets without re-architecting.

---

## Decision

### A. `DUPLICATE_PO` reason codes (binding, V1)

Adopt the 8 codes verbatim from `docs/specs/duplicate-po/calibration-methodology.md`:

```python
INTENT_REASON_TAGS["DUPLICATE_PO"] = (
    "INTENTIONAL_REORDER",
    "AMENDED_PO",
    "BLANKET_RELEASE",
    "SYSTEM_RETRY_VALID",
    "DIFFERENT_SHIP_TO",
    "CONFIRMED_DUPLICATE",
    "PARTIAL_OVERLAP",
    "OTHER",
)
```

`OTHER` is mandatory in every per-intent set as the workflow-safety fallback (already documented in `constraints/specs.py`). `CONFIRMED_DUPLICATE` deliberately included even though it represents agreement with the agent — distinguishing "confirmed" from "no-override" is itself useful training signal (it indicates a human verified the agent's recommendation rather than letting it pass unreviewed).

### B. Per-intent vocabulary takes precedence over global

`INTENT_REASON_TAGS["DUPLICATE_PO"]` is no longer `_GLOBAL_REASON_TAGS`. The framework already documented this fall-back behavior:

> Falls-back behavior: the /disposition endpoint uses the per-intent set when the record carries a known intent, and the global set otherwise (FAILED lifecycle records and any unlisted intent).

This ADR makes that fall-back active for `DUPLICATE_PO`. Other intents continue to fall back to the global set until their own vocabularies are curated.

### C. Lifecycle for adding a per-intent vocabulary

When a new intent ships or an existing intent gains curated reasons:

1. **Source:** product + compliance + (where applicable) ML jointly draft the candidate codes. Calibration use case considered explicitly.
2. **Constraints:** every set must include `OTHER` as the last entry; codes are SCREAMING_SNAKE_CASE; codes are intent-meaningful (not generic).
3. **Storage:** add to `INTENT_REASON_TAGS[<INTENT>]` as a tuple literal in `constraints/specs.py`. Avoid dynamic generation — the literal is the contract.
4. **API exposure:** the existing `/api/v1/health.allowed_override_reason_tags_by_intent` endpoint surfaces the per-intent map automatically; no API change needed.
5. **UI consumption:** `OverrideChooserDialog` reads the per-intent set at render time from health endpoint cache; no per-intent UI work required (see D).
6. **Tests:** vocabulary-sync test in `tests/test_constraints.py` asserts every intent that should have a curated set actually does, and that every set ends with `OTHER`.
7. **Audit:** changes to `INTENT_REASON_TAGS` are versioned in git; no runtime config surface for these (deliberate — reason vocabulary is product policy, not tenant config).

### D. UI behavior (binding)

`asoe-ui/src/app/exceptions/OverrideChooserDialog.tsx`:

1. Reads the per-intent reason set from cached `/health.allowed_override_reason_tags_by_intent`.
2. Renders codes in **3 visual clusters**, not a flat list, when the per-intent set has ≥6 codes. For `DUPLICATE_PO` specifically:

| Cluster | Codes | Meaning |
|---|---|---|
| **Agent was right** | `CONFIRMED_DUPLICATE` | Confirms the recommendation |
| **Agent was wrong** | `INTENTIONAL_REORDER`, `AMENDED_PO`, `DIFFERENT_SHIP_TO`, `BLANKET_RELEASE`, `SYSTEM_RETRY_VALID`, `PARTIAL_OVERLAP` | Override with a structured reason |
| **Edge case** | `OTHER` | Free-text required |

Cluster mapping for `DUPLICATE_PO` is a UI-side constant; mapping for future intents is added when their vocabulary lands.

3. **Free-text notes:** mandatory only when reason is `OTHER`; optional otherwise. Notes always saved alongside the structured code.

4. **Keyboard shortcuts:** structured codes assignable to numeric keys 1–8 in the order shown. CSR power-users can resolve without leaving keyboard. (Implementation detail; ergonomic value only.)

### E. Calibration alignment

The 8 codes were chosen to align with the calibration methodology document's "Override Reason Codes" table. When calibration eventually starts (per ADR-032), the existing tuple of `(reason_code, agent_recommendation, human_decision, signal_breakdown)` is directly consumable as labeled training data — no schema bridge or vocabulary translation needed.

This ADR thus discharges one of ADR-032's "future calibration prerequisites" as part of V1.

---

## Rationale

- **Calibration enablement (ADR-032 dependency):** structured codes are a hard prerequisite for the eventual calibration loop. Free-text alone is not training data.
- **CSR usability (U1):** clustered presentation prevents `OTHER`-fatigue; covers the "agent was right" case explicitly so we capture positive signal as well as negative.
- **Audit value (E5):** every override carries a code + optional note; SOX trail is structured + readable.
- **DDD discipline (E1):** reason codes are domain concepts; they belong in `constraints/specs.py` alongside other domain literals, not as magic strings in UI.
- **Lifecycle clarity:** future intents have a written process for landing their own vocabulary, avoiding "who decides what `BACK_ORDER`'s reasons should be?" debates.

---

## Phased rollout

### V1 (this ADR's scope)

1. Update `constraints/specs.py`:
   - Add `DUPLICATE_PO` reason codes to `INTENT_REASON_TAGS`.
   - Update `AllowedOverrideReasonTag` if necessary (currently a global Literal — V1 keeps it as the union of all per-intent codes; per-intent narrowing happens at API surface, not at the type level).
2. `tests/test_constraints.py`:
   - Update existing vocabulary-sync test to expect `INTENT_REASON_TAGS["DUPLICATE_PO"]` to contain the 8 codes.
   - Add test ensuring `OTHER` is the last entry in every per-intent set.
3. `asoe-ui/src/app/exceptions/OverrideChooserDialog.tsx`:
   - Read per-intent set from `/health.allowed_override_reason_tags_by_intent`.
   - Render in 3 clusters per D for `DUPLICATE_PO`.
   - Make free-text optional except on `OTHER`.
   - Optional: keyboard-shortcut numbering 1–8.
4. Documentation: brief CSR training note describing the 8 codes (lives outside the codebase, owned by CS Operations).

### V1.5

- Manager-facing dashboard of override-reason distribution by customer + analyst, surfacing the early-warning signal for drift (CS Manager U2's request from design review).
- Per-intent reason curation for the next intent that needs it (likely `BACK_ORDER` or `EDI_MISMATCH`).

### V2+

- Multi-language reason-code labels for international CSR teams (codes stay SCREAMING_SNAKE_CASE; labels are localized at the UI layer).
- Calibration consumption of override-reason data per ADR-032.

---

## Consequences

### Positive

- Override workflow becomes data-collection from V1, without waiting for calibration to start.
- CSR experience improves: clustered presentation, optional notes, keyboard shortcuts.
- Future per-intent vocabulary additions follow a documented lifecycle.
- Audit narrative is structured + free-text combined, not either/or.

### Negative

- `OverrideChooserDialog` gains intent-aware rendering logic. Modest complexity; cluster mapping for new intents is a UI-side constant.
- The 8-code set may need refinement after first contact with production traffic. Mitigation: changes go through a git PR (lifecycle step C.7); no runtime config surface to mismanage.
- Reason-code translation (UI label vs storage code) needs to handle codes added between deployments. Standard cache-with-fallback handles this.

### Compliance notes

- Every override now carries a reason code + (where applicable) free-text note; both written to `audit_hash_chain` via `ExecutionLog.resolution_notes` (free-text) and a new `ExecutionLog.resolution_reason_tag` field (structured).
- `resolution_reason_tag` constrained to the intent's allowed set at write time; invalid codes rejected at the API boundary.
- Historical overrides without a reason code (pre-ADR-033) are tagged `LEGACY_NO_REASON` for clean separation; not added to the active vocabulary.

---

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Keep global vocabulary only** | Wastes the calibration-doc's curated vocabulary; makes per-intent training data harder to extract; doesn't fit how analysts actually think about overrides. |
| **Per-customer reason vocabulary** | Calibration training works on per-intent labels, not per-customer; per-customer vocabularies fragment training data unhelpfully. Customer-specific quirks belong in free-text notes, not in the code vocabulary. |
| **Free-text only, no structured codes** | Unusable for calibration. Audit narrative is fine but training-data extraction would require NLP retroactively. Compounds the calibration-deferral cost. |
| **Tenant-configurable reason vocabularies** | Reason codes are product policy, not tenant config. Tenant-configurable codes would fragment the cross-tenant training corpus and create UX inconsistency. Hard reject. |
| **More than 8 codes** | CSR (U1) feedback was that 8 is already the upper bound for ergonomic clustering. More codes would push more overrides into `OTHER`, defeating the structured-code purpose. |

---

## Open questions

- Whether `CONFIRMED_DUPLICATE` should appear in the override dialog at all, given that confirming the agent is technically a *non-override*. Current decision: yes, because explicit confirmation is itself training signal. UI-side decision whether to label that cluster differently ("Confirm" vs "Override"). UX detail; not blocking.
- Whether to add `LEGACY_NO_REASON` to the global vocabulary so historical records with a NULL reason are queryable without special-casing. Lean yes; deferred to implementation.
- Per-intent reason curation cadence — currently ad-hoc per intent. Likely formalize as part of each new-intent ADR going forward.

---

## References

- `constraints/specs.py` (current `INTENT_REASON_TAGS` placeholder)
- `docs/specs/duplicate-po/calibration-methodology.md` (Override Reason Codes section)
- `docs/specs/duplicate-po/2026-05-03-design-review.md` (Item 5)
- `api/routes/health.py` (existing `/health.allowed_override_reason_tags_by_intent`)
- ADR-028, ADR-029, ADR-030, ADR-032
