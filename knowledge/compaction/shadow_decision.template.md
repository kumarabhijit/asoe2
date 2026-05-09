---
event_type: shadow_decision
audit_keys:
  - shadow_verdict
  - intent
  - recommended_action
  - amount_usd
---
# Compaction template — shadow_decision

ADR-038 Phase H.7 / §11.2. One line per Compliance Shadow
verdict (L1 deterministic OR L2 LLM second opinion when ADR-039
X.1+ is invoked).

## Audit signal

* **shadow_verdict** — `GREEN` / `YELLOW` / `RED`. The single
  load-bearing field per CLAUDE.md §4.
* **intent** — the AllowedIntent literal the verdict applies to.
* **recommended_action** — the recipe's proposal at the time
  of the verdict; helps the reviewer see what was being gated.
* **amount_usd** — the financial impact at decision time;
  drives the four-eyes / cosign threshold downstream.

## What's deliberately omitted

* `policy_hits` — verbatim list. Retained in the raw event
  for the reviewer who clicks through; compacted summary keeps
  the line scannable.
* L2 LLM Shadow's `reason` and `policy_concerns` — these are
  audit-bearing on the verdict itself but verbose; reviewer
  pulls them from the raw event when investigating a specific
  downgrade.

## Replay invariant

Two shadow_decision lines with the same input dict produce the
same compacted line. ADR-039 §7.4 replayability sits on top of
this guarantee.
