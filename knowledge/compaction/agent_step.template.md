---
event_type: agent_step
audit_keys:
  - outcome
  - tool_name
  - status
  - case_status_after
  - reason_code
---
# Compaction template — agent_step

ADR-038 Phase H.7 / §11.2. One line per agent loop iteration.
The summarised line tells a Compliance reviewer:

* Which tool the agent invoked (`tool_name`).
* Whether it succeeded (`status` = `ok` / `error`).
* Where the case ended up after the call (`case_status_after`).
* The semantic reason for any halt (`reason_code` from the L0
  vocabulary; only present on `escalate` / `request_clarification_email`
  halts).
* The aggregate `outcome` of the step (RESOLVED / ESCALATED /
  AWAITING_BUYER / AWAITING_ERP / BUDGET_EXHAUSTED / ERROR).

## Why these keys

* **outcome / status** are the two halt-vs-continue signals the
  reviewer scans for first.
* **tool_name** anchors the line in the §6.4 tool surface so the
  reviewer can cross-reference the recipe registry without
  reading the tool_call body.
* **case_status_after** drives downstream-state expectations —
  the reviewer can verify the next step's `case_status_before`
  matches.
* **reason_code** is in the closed override-reason vocabulary
  (ADR-033) so it's auditable without prose.

## What's deliberately omitted

* Tool arguments — verbose, PII-bearing in some cases. Retained
  verbatim in `case_events.tool_call` for auditors who need them.
* LLM input/output — never enters compaction. Provider trace
  is in `LLMCallTrace` if needed.
* Free-form `result.error` strings — the `status="error"` flag
  is enough at the compacted level; the verbatim error stays in
  the raw event.

## Replay invariant

Audit-keys appear in the order declared in this template's
frontmatter. Reordering the list produces a different
compaction summary on replay, which the
`tests/test_compaction_sla_backfill.py::test_replay_diverges_*`
suite catches at PR review time.
