---
event_type: sla_breach
audit_keys:
  - case_status_after
  - reason_code
  - amount_usd
  - autonomy_level
learning_signals:
  - breach_rate_per_customer_tier:
      describes: SLA breach incidence per Strategic / Mid-Market / Long-tail
      trains: tier-policy refinement
              (knowledge/policy/sla_per_customer_tier.yaml)
  - breach_correlation_with_intent:
      describes: which intents disproportionately breach SLA
      trains: per-intent processing-time SLA review
---
# Compaction template — sla_breach

ADR-038 Phase H.7 / §11.2. Emitted by the SLA monitor when a
case crosses its `sla_deadline` without a terminal disposition.

## Audit signal

* **case_status_after** — typically `OPEN_AWAITING_HUMAN`
  (review queue) or unchanged (the breach itself doesn't move
  the case; it's a timer event).
* **reason_code** — `sla_breach` (the breach itself) or a
  more specific L0 vocabulary entry when the breach correlates
  with a known escalation pattern.
* **amount_usd** — financial impact at breach time. Drives
  exception-tier alerting downstream.
* **autonomy_level** — the level the breach occurred at;
  high-autonomy breaches are louder alerts.

## Why not include `hours_late`

The temporal delta is computable from the event timestamp +
the case's `sla_deadline` — including it in the compacted line
duplicates information already in the raw event without buying
auditor signal.
