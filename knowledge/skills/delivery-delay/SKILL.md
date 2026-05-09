---
name: delivery-delay
description: Classifies shipment delay severity (SD-DELAY-001 minor / SD-DELAY-002 severe) and ranks expedite / split-ship / reschedule options. Triggered on DELIVERY_DELAY events.
metadata:
  version: 1.0.0
  author: CPG Expert-Systems-Architect
  required_tools: [mcp-oms-connector, mcp-carrier-integration]
  recipes: [DeliveryDelayResolutionRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Delivery Delay Resolution

## 1. Overview
Triggered when a shipment's projected ETA slips past the planned
delivery date. The skill classifies the delay as MINOR or SEVERE and
ranks alternate options (EXPEDITE, SPLIT_SHIP, RESCHEDULE). Days-late
thresholds live in `DeliveryDelayResolutionRecipe.py` and are
policy-injected from `contracts/policy.py`.

## 2. Reasoning Loop
1. Confirm event_type is `DELIVERY_DELAY`.
2. Read `metadata.planned_date`, `metadata.projected_eta`, optionally
   `metadata.alternate_options` / `delay_category` / `carrier` /
   `route`.
3. Classify intent as `DELIVERY_DELAY`.
4. Select `DeliveryDelayResolutionRecipe.py`.
5. Compliance Shadow runs before the recipe.

## 3. Constrained Generation Policy
- intent must be `DELIVERY_DELAY`
- recipe must be `DeliveryDelayResolutionRecipe.py`
- shadow verdict `GREEN | YELLOW | RED`
- option type rendered verbatim (`EXPEDITE | SPLIT_SHIP | RESCHEDULE`
  plus caller-supplied variants); no UI dispatch on the value.

## 4. Recipe-to-Intent Mapping
- DELIVERY_DELAY → `DeliveryDelayResolutionRecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
- GREEN: ON_TIME (delta < `DELIVERY_DELAY_MINOR_DAYS`).
- YELLOW: MINOR_DELAY (2–4 days); operator picks from ranked options.
- RED: SEVERE_DELAY (≥ `DELIVERY_DELAY_SEVERE_DAYS`); reschedule /
  buyer notification mandatory. For SEVERE delays the option ranker
  prefers RESCHEDULE over EXPEDITE to avoid expensive last-mile work.

## 6. Output Requirements
- `days_late`, `delay_category`
- `classification` (ON_TIME | MINOR_DELAY | SEVERE_DELAY)
- `primary_option` + full `alternate_options[]`
- `recommended_action` (EXPEDITE | SPLIT_SHIP | RESCHEDULE | NO_ACTION)
