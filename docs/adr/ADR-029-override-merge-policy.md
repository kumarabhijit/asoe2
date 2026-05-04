# ADR-029: Override Merge & Renormalization Policy for Detection Weights

**Status:** Accepted (revisions per 2026-05-10 review)
**Date:** 2026-05-03 (initial); 2026-05-10 (revisions)
**Deciders:** Same as ADR-028 (review session 2026-05-03; sign-off review 2026-05-10).
**Applies to:** `recipes/DuplicatePORecipe.py`, `contracts/policy.py`, `gateways/tenant_config.py` (new), `orchestration/nodes.py::validate_types`, `tests/test_recipes.py`.
**Related:** ADR-028 (storage shape), ADR-030 (5-level config hierarchy), ADR-032 (calibration deferral).

---

## Context

The new duplicate-PO spec defines a 5-level config override hierarchy (platform → tenant → customer-tier → customer-specific → customer-channel). Each level can supply *partial* score-weight maps. Example from `gateways/configs/duplicate_po/defaults.json`:

```json
"customer_behavior_overrides": {
  "blanket_po": {
    "score_weights_override": { "po_number": 0.10, "line_items": 0.35 }
  },
  "drop_ship": {
    "score_weights_override": { "ship_to": 0.00, "line_items": 0.10 }
  }
}
```

Two problems surface:

1. The recipe (`recipes/DuplicatePORecipe.py:_WEIGHTS`) asserts `sum(_WEIGHTS) == 1.0` at module load. Partial overrides break this invariant.
2. Different merge semantics produce different — and silently different — scores. A customer expecting "I overrode 2 weights, the other 6 stay the same" is not the same customer as one expecting "I overrode 2 weights, the other 6 rebalance to keep sum=1."

Without a written policy, behavior is whatever the implementation happens to do, and `customer_behavior_overrides` becomes a debugging nightmare the first time a customer's calibrated weights produce unexpected scores.

Three options surfaced in the design review:

- **(a) Customer config supplies all 8 weights, runtime fills nothing.** Strict; collapses inheritance; noisy config files.
- **(b) Customer config supplies partial weights, engine renormalizes proportionally to keep sum=1.** Convenient; produces surprising rebalancing the customer didn't ask for.
- **(c) Customer config supplies partial weights, engine fills missing keys from the next-higher level in the hierarchy, then asserts sum=1.** Honest with the inheritance model; fails closed on misconfiguration.

ML lens noted that calibration outputs (when they eventually arrive — see ADR-032) will be *deltas from a tier baseline*, not full vectors. Admin lens preferred `{po_number: 0.10, line_items: 0.35}` over `"please specify all 8 every time."` DDD lens called (c) the only option consistent with the inheritance model defined in ADR-030.

---

## Decision

**Adopt option (c): hierarchical layered merge, validate, fail closed.**

### Algorithm

```
def resolve_weights(
    platform_weights: dict[str, float],
    tenant_overrides: dict[str, float],
    tier_overrides: dict[str, float],
    customer_overrides: dict[str, float],
    channel_overrides: dict[str, float],
) -> dict[str, float]:
    merged = dict(platform_weights)
    for layer in (tenant_overrides, tier_overrides, customer_overrides, channel_overrides):
        merged.update(layer)
    _validate_keys(merged)
    _validate_sum_to_one(merged, tol=1e-4)
    return merged
```

Trace emitted alongside the resolved map records *which layer contributed each final value* for diagnostic transparency (see ADR-030 for trace shape).

### Validation rules (binding)

1. **Key set:** the merged map must contain exactly the eight signal keys defined in `recipes/DuplicatePORecipe.py:_WEIGHTS` (`po_number`, `customer_id`, `line_items`, `amount`, `timestamp`, `ship_to`, `channel`, `delivery_date`). Extra keys raise `WeightContractViolation`. Missing keys after merge are impossible because the platform layer is complete.
2. **Numeric range:** each weight must be in `[0.0, 1.0]`. Negative or >1.0 values raise `WeightContractViolation`.
3. **Sum constraint:** `abs(sum(merged.values()) - 1.0) <= 1e-4`. Violation raises `WeightContractViolation`.

   > **2026-05-10 review revision (E6, E2, E1):** Tolerance was originally `1e-6`, revised to `1e-4`. Rationale: logistic-regression weight outputs after normalization commonly land at `±1e-4` to `±1e-5` due to floating-point accumulation. A `1e-6` tolerance would cause real calibrated deliveries to fail validation on arrival. `1e-4` is the right scale for "did a numerical pipeline produce slightly imprecise output," not for "did the developer hand-typed the wrong number" (the latter is caught well below `1e-4`).
   >
   > The looseness is *intentional* — driven by floating-point reality, not by softening the weight-sum honesty rule. Future contributors must not tighten this back without revisiting the calibration-pipeline contract.
4. **No silent renormalization.** The engine never rescales values. If a customer override produces a non-1.0 sum (within tolerance), that is a configuration bug and must be surfaced, not papered over.

### Failure mode (binding)

On `WeightContractViolation`:

1. The detection for that event falls back to `platform_weights` only (the platform-default map is always sum-to-1 by the module-load assertion).
2. The fallback is recorded in the audit chain with `reason: "WEIGHT_CONTRACT_VIOLATION"` and the violating layered configuration.
3. A `config_validation_alert` event is emitted with `severity: ERROR`, surfaced in the admin config UI for the affected `(tenant, customer_tier, customer, channel)` quadruple.

   > **2026-05-10 review revision (U3):** Alert *delivery channel* (email vs Slack vs dashboard vs all) is intentionally not pinned at the ADR layer — that's a notification-system decision. The ADR commits to: alert is emitted via the existing alert pipeline AND surfaced in the admin config UI for the affected configuration scope. Implementation PR must make the channel concrete (admin config UI is the minimum bar; dashboard / Slack / email are the standard pipeline wiring).

4. The recipe still produces a result; no event is dropped on the floor.

This is **fail closed** in the right direction — the system continues to operate with safe defaults rather than blocking inbound POs while admins fix config.

### Recipe signature change

`recipes/DuplicatePORecipe.py::detect_duplicate_po` gains an optional `weights` parameter:

```python
def detect_duplicate_po(
    incoming_po_number: str,
    customer_id: str,
    signal_scores: Dict[str, float],
    threshold_auto_block: float = 0.90,
    threshold_review_required: float = 0.70,
    threshold_soft_flag: float = 0.50,
    original_fulfilled: Optional[bool] = None,
    has_revision_indicator: Optional[bool] = None,
    line_items_identical: Optional[bool] = None,
    autonomy_levels: Optional[Dict[str, str]] = None,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    effective_weights = weights if weights is not None else _WEIGHTS
    _assert_weight_contract(effective_weights)
    ...
```

When `weights is None`, behavior is unchanged from V0 (module-default map). The runtime assertion replaces the module-load assertion as the source-of-truth gate for runtime weights but the module-load assertion stays in place to catch misedits to `_WEIGHTS` itself.

### Where merge happens

The merge runs in `gateways/tenant_config.py::resolve_for_event` (new gateway, registered as a `GatewayDependency` on `recipes/registry.py:DuplicatePORecipe.py`). The resolved weight map enters the recipe via `state.resolved_data["tenant_config"].weights` and is plumbed to `detect_duplicate_po(weights=...)` by `orchestration/nodes.py::validate_types`. The recipe stays pure (no I/O, no merge logic, just validation and scoring).

---

## Rationale

- **Inheritance honesty:** Option (c) is the only semantics consistent with the 5-level hierarchy defined in ADR-030. (a) collapses inheritance; (b) silently rebalances. Both undermine admin trust in what they wrote.
- **Calibration alignment:** ML calibration deliverables will be deltas from a tier baseline. (c) accepts deltas natively; (a) forces full-vector deliverables; (b) rebalances calibrated values into something different. Tolerance `1e-4` accepts the floating-point reality of those deliveries.
- **Fail-closed safety:** A misconfiguration cannot silently produce skewed scores — either validation passes and the explicit weights run, or it fails and platform defaults run. There is no third possibility.
- **Observability:** Per-layer contribution trace makes "why did this customer get those weights?" answerable from the audit envelope, not from re-reading config files.

---

## Phased rollout

### V1 (this ADR's scope)

1. Add `_assert_weight_contract` to `recipes/DuplicatePORecipe.py`; promote sum/key/range checks from module-load to runtime; tolerance `1e-4`.
2. Add `weights` param to `detect_duplicate_po`; default `None` → use `_WEIGHTS`.
3. Implement `gateways/tenant_config.py` with `resolve_for_event(tenant_id, customer_id, customer_tier, channel) -> ResolvedConfig` — V1 reads from `gateways/configs/duplicate_po/defaults.json` on disk; production-grade backing store deferred (see ADR-030).
4. Register `tenant_config` `GatewayDependency` on `DuplicatePORecipe.py` in `recipes/registry.py`. Result key: `tenant_config`.
5. Extend `orchestration/nodes.py::validate_types` for `DuplicatePORecipe.py` to extract `weights` from `state.resolved_data["tenant_config"]`.
6. Emit per-layer contribution trace in `tenant_config` gateway response for the audit envelope (consumed by ADR-028 Guard-rail 2 read API).
7. Emit `config_validation_alert` to existing alert pipeline + admin config UI (per 2026-05-10 revision).
8. Tests:
   - `tests/test_recipes.py::TestDuplicatePORecipe::test_custom_weights_override` — supply weights summing to 1.0, verify scoring uses them.
   - `tests/test_recipes.py::TestDuplicatePORecipe::test_weights_sum_violation_raises` — supply weights summing to 0.95, expect `WeightContractViolation`.
   - `tests/test_recipes.py::TestDuplicatePORecipe::test_weights_floating_point_tolerance` — supply weights summing to `1.0 + 5e-5`, verify accepted (within `1e-4` tolerance); supply summing to `1.0 + 5e-4`, verify rejected.
   - `tests/test_recipes.py::TestDuplicatePORecipe::test_weights_negative_value_raises` — supply a negative weight, expect violation.
   - `tests/test_recipes.py::TestDuplicatePORecipe::test_weights_unknown_key_raises` — supply an extra key, expect violation.
   - `tests/test_tenant_config_gateway.py::test_layered_merge_partial_overrides` — platform + tier + customer-channel partial maps, verify merge order and per-layer trace.
   - `tests/test_tenant_config_gateway.py::test_invalid_merged_config_falls_back_to_platform` — customer override produces sum=0.95, verify fallback to platform defaults + audit-chain entry + alert emission.

### V1.5

- Surface `config_validation_alert` events in the admin config UI as a richer interactive surface (V1 surfaces them; V1.5 builds the management workflow around them).
- Add per-recipe weight contracts for other intents that grow override surfaces (e.g., `EdiMismatchRecipe` autonomy-level overrides).

---

## Consequences

### Positive

- Override semantics are explicit, documented, and testable.
- Calibrated-weight deliverables can be partial deltas without surprise; tolerance `1e-4` accepts realistic floating-point output.
- Misconfigurations are surfaced as alerts, not silent score skew.
- Recipe stays pure; I/O and merge live in the gateway.

### Negative

- One more gateway dependency on `DuplicatePORecipe.py`. Adds a small latency budget (file read in V1; cache later).
- Admins must understand "no renormalization" — config files that sum to 0.95 will fail validation rather than be silently rescaled. Documentation onus.
- The runtime weight assertion shifts the failure point from module-load to per-event. Tests must cover the per-event failure path.
- Tolerance `1e-4` is a "soft" bound by precision standards; documenting *why* it's that loose is on the team to maintain.

### Compliance notes

- Every weight-contract violation is captured in the audit chain with full violating-config payload. SOX-traceable.
- The resolved weight map for any historical detection is reconstructable from the per-layer trace stored in `audit_hash_chain`, not just the final score.

---

## Alternatives considered

| Option | Why rejected |
|---|---|
| (a) Customer must supply full 8 weights | Collapses the inheritance model defined by ADR-030; defeats the purpose of layered overrides; noisy config files; misaligned with how calibration deliverables will arrive. |
| (b) Engine renormalizes proportionally | Produces silent rebalancing the customer did not author. ML lens objection: calibrated weights become "calibrated weights, then post-hoc rescaled by a coefficient nobody asked for." Trust-eroding. |
| Soft-fail (warn but proceed with violating weights) | Defeats the entire purpose of the contract. SOX-incompatible. Rejected. |
| Hard-fail (drop the event entirely on violation) | Inbound POs are time-sensitive; blocking ingestion on config bugs trades a bad outcome for a worse one. Fall-back-to-platform-defaults is the right balance. |
| Tolerance `1e-6` | Rejected per 2026-05-10 review: floating-point reality of calibration outputs lands at `±1e-4` to `±1e-5`; a `1e-6` tolerance would reject real calibrated deliveries on arrival. |

---

## Open questions

- Whether `customer_behavior_overrides` (blanket_po, drop_ship, high_frequency from the spec's config) should layer below or beside `customer_specific_overrides`. Current decision: they are *applied to the customer-specific layer* by the admin tooling (admin tags a customer as "blanket_po" and the override is materialized into customer-specific config). Treating them as a sixth layer is rejected to keep the hierarchy at 5. To be confirmed in ADR-030.

---

## Notes (non-blocking)

- The `drop_ship` preset in `gateways/configs/duplicate_po/defaults.json` sets `line_items: 0.10` (lower than default 0.20). Direction looks counter-intuitive to ERP veteran reviewer ("drop-ship usually wants line-items emphasized, not de-emphasized"); flagged for product owner review but not blocking — admin can override per customer.

---

## References

- `gateways/configs/duplicate_po/defaults.json`
- `docs/specs/duplicate-po/calibration-methodology.md` (informs the future direction; calibration itself deferred per ADR-032)
- `docs/specs/duplicate-po/2026-05-03-design-review.md` (Item 4)
- `docs/specs/duplicate-po/2026-05-10-adr-review.md` (revisions)
- ADR-028, ADR-030, ADR-032
