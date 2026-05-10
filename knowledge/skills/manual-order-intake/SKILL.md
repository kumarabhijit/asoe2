---
name: email-order-entry
description: Classifies a post-extraction email-channel sales-order envelope into one-click approve, standard review, low-confidence, or fatal-reject. Triggered on EMAIL_ORDER_ENTRY_REQUEST events.
metadata:
  version: 1.0.0
  author: ASOE Order-Management Architect
  required_tools: [mcp-erp, mcp-pinecone-search]
  recipes: [EmailOrderEntryRecipe.py]
  constrained_generation: [Guidance, Outlines]
---
# Skill: Email Order Entry (Non-EDI)

## 1. Overview
This skill is triggered when the upstream `email-intelligence-agent` classifies an
inbound email as a non-EDI purchase order and produces an extracted, validated envelope.
The skill classifies the envelope's terminal action on the email-intake leg of the
order-to-cash pipeline. All scoring thresholds and the four "non-disable-able floor"
rules live in `EmailOrderEntryRecipe.py`; tenant overrides are deferred to the
`tenant_config` gateway per ADR-030.

## 2. Reasoning Loop
1. Identify that the event is an email-order intake (event_type:
   `EMAIL_ORDER_ENTRY_REQUEST`).
2. Confirm `event.metadata.composite_confidence` and
   `event.metadata.non_disableable_floor` are present.
3. Classify intent as `EMAIL_ORDER_ENTRY`.
4. Select `EmailOrderEntryRecipe.py` — the only allowed recipe for this intent.
5. Note that orchestration will resolve gateway dependencies before recipe execution
   (the four non-disable-able floor checks plus the expensive validation primitives).
   Do not call any of these gateways directly.
6. Do not call any recipe before the Compliance Shadow has returned GREEN.
7. Do not improvise the logic.

## 3. Constrained Generation Policy
Use Guidance / Outlines for all machine-consumed outputs:
- intent label must be `EMAIL_ORDER_ENTRY` (the only valid value for this event type)
- recipe name must be `EmailOrderEntryRecipe.py` (registered recipe, no other allowed)
- shadow verdict must be constrained to GREEN / YELLOW / RED
- `recommended_action` must be one of `AllowedResolutionAction`

## 4. Recipe-to-Intent Mapping
- Email order intake → `EmailOrderEntryRecipe.py`
- All other intents on this event type → `FAIL_TO_HUMAN`

## 5. Execution Protocol
Before calling any recipe, check the Compliance Shadow.
If the shadow returns RED, halt execution and explain the compliance breach.
If the shadow returns YELLOW, route to MANUAL_REVIEW_REQUIRED.

## 6. Output Requirements
Always include the Recipe Execution Log snippet for audit traceability.
The log must capture: composite_confidence, classification, recommended_action,
autonomy_level, validation_failures, floor_breaches, and (when terminal-rejected)
reject_reason_code.
