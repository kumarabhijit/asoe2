---
event_type: tool_call
audit_keys:
  - tool_name
  - status
  - case_status_after
  - amount_usd
  - reason_code
---
# Compaction template — tool_call

ADR-038 Phase H.7 / §11.2. Direct `(tool_call, tool_result)`
events emitted by the L4 harness's `ToolCallReplayLog`. Distinct
from `agent_step` (which is the loop iteration as a whole); a
single `agent_step` may contain multiple `tool_call` rows.

## Audit signal

* **tool_name** — the §6.4 tool that ran.
* **status** — `ok` / `error`. The compaction does not retain
  the verbatim error string.
* **amount_usd** — present when the tool moves money or
  recommends a financially-binding action; surfaced so the
  reviewer can see the financial trail without re-reading
  every tool result.
* **case_status_after** — when the tool was a halt-tool
  (`declare_done`, `escalate`, `request_clarification_email`),
  this is the new case status the harness applied.
* **reason_code** — semantic reason from the closed L0 override
  vocabulary (ADR-033).

## Compliance-reviewer narrative

Reading a sequence of compacted `tool_call` lines should let the
reviewer reconstruct: which tools fired, in which order, with
what financial impact at each step, and how the case state
evolved.
