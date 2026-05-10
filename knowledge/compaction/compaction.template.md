---
event_type: compaction
audit_keys:
  - reason_code
  - case_status_after
---
# Compaction template — compaction (recursive)

ADR-038 Phase H.7 / §11.2. The compaction process itself is an
audit-log event: when a case's working memory is compacted, the
event is recorded so subsequent compactions can be correlated.

## Audit signal

* **reason_code** — which trigger fired (`token_budget` /
  `event_count` / `age_days` per ADR-038 §7.4).
* **case_status_after** — current status at compaction time;
  surfaces when compaction is firing on long-running awaiting-
  buyer / awaiting-ERP cases.

## What's deliberately omitted

* `events_summarised` count — derivable from the working memory
  delta; the auditor doesn't need it to reason about whether
  compaction was correctly triggered.
* `summary_text` — by definition the post-compaction view; not
  audit-bearing on the trigger event itself.

## Replay note

Compaction events are byte-identical on replay only when the
trigger inputs (token estimate, event count, case open age) are
byte-identical. The
`tests/test_compaction_sla_backfill.py::TestCompactionRunner`
suite verifies this round-trip explicitly.
