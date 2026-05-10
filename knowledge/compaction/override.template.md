---
event_type: override
audit_keys:
  - resolved_action
  - reason_code
  - amount_usd
  - case_status_after
learning_signals:
  - reason_code_clusters:
      describes: empirical clustering of override reasons per intent
      trains: Phase 5.1 per-intent override-reason-tag curation
              (the load-bearing input for the §5.1 review template)
  - reviewer_override_rate_on_llm_downgrades:
      describes: rate at which a reviewer reverses an LLM_SHADOW
                 downgrade — the §7.3 SLI driving X.2 rollback
      trains: ADR-039 X.2 → X.3 ratification gate
  - amount_at_override:
      describes: financial impact distribution at human override
      trains: cosign-threshold calibration (current $10k)
---
# Compaction template — override

ADR-038 Phase H.7 / §11.2. Human-triggered override events
(four-eyes / cosign / disposition). One line per override
action recorded on the case.

## Audit signal

* **resolved_action** — the AllowedResolutionAction literal the
  human chose.
* **reason_code** — the L0 override-reason-vocabulary entry
  (ADR-033). Free-form `resolution_notes` are retained on the
  raw event but not in compaction.
* **amount_usd** — financial impact of the override; drives
  the cosign threshold.
* **case_status_after** — where the case landed.

## SOX bearing

Override events are SOX-relevant: the resolved_by user, the
reason_code, and the amount together prove the management
override control held. The compacted line preserves the
amount + reason; the raw event preserves resolved_by +
notes for the auditor's investigation.
