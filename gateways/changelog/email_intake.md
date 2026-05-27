# `email_intake` connector — CHANGELOG

Schema-drift attribution trail for the live Microsoft Graph backend
(`gateways/msgraph_intake.py`). One row per fixture refresh / contract
change per `docs/ops/fixture-capture.md` cadence.

Format: `YYYY-MM-DD | operation(s) | captured-by | reason`

---

## 2026-05-27 — initial scaffolding

`PARITY-6.1` — `GraphIntakeGateway` live-or-stub router wired behind
`ASOE_EMAIL_INTAKE_DRIVER=graph`. Default `recorded` keeps traffic on
the existing sandbox stub. Per-operation routing through
`gateways.shadow_mode.ShadowRunner` + `gateways.canary.is_canary_eligible`;
terminal live failures dead-letter to `api.dead_letter_queue` so the
operator dashboard surfaces orphans without losing the recipe path.

* Operations supported by the live backend: `sender_auth`,
  `resolve_customer`, `duplicate_po_pre_check`, `credit_check`,
  `fetch_message`.
* Field classification (used by the shadow-runner diff buckets):
  * audit-bearing: `sender_authorized`, `customer_resolved`,
    `customer_name`, `duplicate_po_clear`, `matched_po_id`,
    `credit_clear`, `credit_limit`, `source_email_id`,
    `from_address`, `received_at`.
  * derived: `auth_method`, `auth_evidence`, `match_method`,
    `match_confidence`, `match_score`, `current_exposure`,
    `headroom`, `subject`, `body_excerpt`, `body_hash`,
    `attachment_manifest`.
* `fetch_message` body egress runs through
  `gateways.azure_di_egress_redaction.redact_for_azure_di` before the
  bytes leave our perimeter (Security + ML review requirement).

No fixtures committed yet — the recorded backend is the existing
`StubGateway("email_intake", …)` in `api/sandbox_gateways.py`; weekly
`scripts/record_gateway.py` captures land under
`gateways/fixtures/email_intake/` once the platform team provisions
the `asoe-fixtures.onmicrosoft.com` sandbox tenant
(`docs/ops/fixture-capture.md` row 1).
