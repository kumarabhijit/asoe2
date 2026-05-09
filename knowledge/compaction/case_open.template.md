---
event_type: case_open
audit_keys:
  - source
  - source_channel
  - tier
  - sla_deadline
---
# Compaction template — case_open

ADR-038 Phase H.7 / §11.2. The materialisation event recorded
when an OrderCase is opened (eagerly for Manual Orders; lazily
on first non-clean event for Automated Orders).

## Audit signal

* **source** — `manual_order` / `automated_order` (immutable per
  §3.1). Determines whether prose extraction was needed.
* **source_channel** — finer-grained channel (email / phone /
  fax / edi_x12_850 / portal / api_feed / ftp_csv / ftp_xml /
  vmi_replenishment).
* **tier** — initial materialisation tier (T2 by default;
  Manual Orders open at T2 eagerly, Automated open at T2 lazily
  on non-clean events).
* **sla_deadline** — the customer-tier-driven deadline stamped
  at open time. Drives the SLA-breach event downstream when
  exceeded.

## Replay invariant

Case-open events are byte-identical at open time and at replay
because all four fields are deterministic functions of the
inbound event + customer profile.
