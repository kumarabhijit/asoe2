---
name: price-hold-release
description: Decides the disposition of an EDI 850 order line that is currently held because the PO price is outside tolerance of the SAP base price. Triggered on EDI_850_PRICE_HOLD events.
metadata:
  version: 1.0.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-sap-connector, mcp-oms-connector]
  recipes: [PriceHoldReleaseRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Price Hold Release

## 1. Overview
This skill is triggered when an inbound EDI 850 line is currently held because
the PO price variance from SAP base price requires a release decision. It is
distinct from **contractual correction**, which changes the applied price —
this skill decides whether to **lift an existing hold** (auto-release,
escalate, or hard-block).

Do not improvise tolerance logic. All tolerance / hard-block thresholds live
in `contracts/policy.py` and are consumed by `PriceHoldReleaseRecipe.py`.

## 2. Reasoning Loop
1. Confirm event_type is `EDI_850_PRICE_HOLD`.
2. Confirm `event.metadata.price_hold_status == "HELD"`.
3. Classify intent as `PRICE_HOLD_RELEASE`.
4. Select `PriceHoldReleaseRecipe.py` — the only allowed recipe for this intent.
5. Do not call the recipe before the Compliance Shadow has returned a verdict.

## 3. Constrained Generation Policy
Use Guidance / Outlines for all machine-consumed outputs:
- intent label must be `PRICE_HOLD_RELEASE`
- recipe name must be `PriceHoldReleaseRecipe.py`
- shadow verdict must be constrained to GREEN / YELLOW / RED
- recipe output `action` must be one of `AllowedPriceHoldAction`
  (AUTO_RELEASE / ESCALATE / HARD_BLOCK)

## 4. Recipe-to-Intent Mapping
- PRICE_HOLD_RELEASE → `PriceHoldReleaseRecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
Compliance Shadow runs before the recipe. Verdicts:
- GREEN → variance within tolerance; auto-release permitted.
- YELLOW → variance above tolerance but within hard-block; route to
  MANUAL_REVIEW_REQUIRED.
- RED → variance exceeds hard-block; halt execution and return REJECTED.

The recipe never reads `contracts.policy` directly; thresholds are injected
by orchestration from policy so the same logic serves different customer /
vendor threshold sets.

## 6. Output Requirements
The Recipe Execution Log must capture:
- `variance_pct` (signed fraction)
- `action` (AUTO_RELEASE | ESCALATE | HARD_BLOCK)
- `tolerance_pct` and `hard_block_pct` applied
- `reason` (human-readable policy hit)
