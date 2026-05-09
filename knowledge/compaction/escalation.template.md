---
event_type: escalation
audit_keys:
  - reason_code
  - target_role
  - autonomy_level
  - amount_usd
---
# Compaction template — escalation

ADR-038 Phase H.7 / §11.2. Case escalations — agent's `escalate`
tool firing, or harness-level lifecycle escalation when a
budget exhaustion / shadow-RED routes the case out of automated
handling.

## Audit signal

* **reason_code** — closed L0 vocabulary entry (ADR-033). Always
  present on agent-driven escalations.
* **target_role** — `analyst` / `manager` / `admin` (RBAC role
  the case routes to).
* **autonomy_level** — the autonomy tier the escalation
  exceeded (L1 / L2 / L3 / L4 per the per-intent autonomy maps).
* **amount_usd** — financial impact at escalation time.

## What's deliberately omitted

* Free-form prose explanations. The reason_code is the
  audit-bearing token.
