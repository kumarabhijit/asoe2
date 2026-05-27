# `oms` connector — CHANGELOG

Schema-drift attribution trail for the live OMS backend
(`gateways/oms_live.py`). One row per fixture refresh / contract
change per `docs/ops/fixture-capture.md` cadence.

Format: `YYYY-MM-DD | operation | captured-by | reason`

---

## 2026-05-27 — initial scaffolding

`PARITY-6.4` — `OmsGateway` live-or-stub router wired behind
`ASOE_OMS_DRIVER=live`. Default `recorded` keeps the OMS connector
on the existing sandbox stub. Same shadow + canary + DLQ pattern
Graph 6.1 + SAP 6.3 use.

OMS-specific addition vs the read-only connectors: a
`record_post_success_orphan(tenant_id, operation, recipe_run_id,
reason)` helper for the post-recipe-success failure window. When a
recipe completes GREEN (audit log records "order accepted") but the
subsequent OMS write fails (502 after retry, inventory race, …) the
helper drops a `source="oms"` DLQ row carrying the recipe_run_id so
an operator can correlate the orphan back to the recipe execution
and reconcile.

Operations:

* `get_fulfillment_status` — read.
* `get_inventory_snapshot` — read; multi-warehouse + substitutes.
* `get_matched_po_details` — read; duplicate-PO detector evidence.
* `get_price_hold_status` — read.
* `write_order_acceptance` — write (post-resolution).
* `write_order_cancellation` — write (post-resolution).

Field classification (used by the shadow-runner diff buckets):

* audit-bearing: `fulfilled`, `primary_dc`, `atp_date`,
  `has_revision_indicator`, `line_items_identical`,
  `cancellation_target`, `customer_id`, `status`, `oms_order_id`,
  `cancelled_at`.
* derived: `alternate_warehouses`, `substitutes`, `production`,
  `inbound_po`, `days_between`, `detection_method`, `matching_fields`,
  `differing_fields`, `original_order`, `duplicate_order`.

`LiveOmsBackend.execute` raises `NotImplementedError` on the
red-green path — live HTTP transport lands behind the nightly
`-m live` mark.

Fixture capture: per `docs/ops/fixture-capture.md` row 3, OMS
fixtures come from the internal `oms-preprod` cluster against
synthetic orders only. Production OMS captures are forbidden.
