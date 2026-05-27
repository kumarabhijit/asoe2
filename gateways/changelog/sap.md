# SAP S/4HANA connectors — CHANGELOG

Schema-drift attribution trail for the live S/4HANA backend
(`gateways/sap_live.py`). One row per fixture refresh / contract
change per `docs/ops/fixture-capture.md` cadence.

The seven SAP domains share this CHANGELOG because they share the
``LiveSapBackend`` (single OAuth + OData context per Decision Q3).
Per-domain rows distinguish which contract drifted.

Format: `YYYY-MM-DD | connector.operation | captured-by | reason`

---

## 2026-05-27 — initial scaffolding

`PARITY-6.3` — `SapDomainGateway` live-or-stub router wired behind
`ASOE_SAP_DRIVER=s4hana`. Default `recorded` keeps the seven SAP
domains on their existing sandbox stubs. `api/preprod_gateways.py`
swaps every domain through the router when the driver env is set;
canary percentage is shared across the seven domains under
`ASOE_CANARY_PCT_SAP`.

Seven domains routed:

* `sap_order` — validate (SO confirmation + ATP).
* `sap_doc` — lookup (sales-doc + condition chain).
* `sap_contract` — lookup (contract ref + rule id).
* `promotion` — lookup (promotion ref).
* `sap_block` — lookup (delivery block status).
* `sap_customer_master` — lookup (MOQ source + channel).
* `sla_contract` — lookup (SLA deadline + at-risk amount).

Field classification (used by the shadow-runner diff buckets):

* audit-bearing: `system`, `validation_status`, `sap_doc_number`,
  `doc_type`, `doc_number`, `applied_condition_chain`,
  `contract_ref`, `rule_id`, `root_cause_category`,
  `promotion_ref`, `block_status`, `block_reason`, `moq_source`,
  `channel`, `sla_deadline`, `at_risk`.
* derived: `order_value_usd`, `sku`, `uom`, `material_desc`,
  `order_date`, `block_message`.

Terminal live failure (OData pool exhaustion, 5xx after retry) →
`api.dead_letter_queue.record(source="sap", ...)` so the operator
dashboard groups every SAP-driven orphan together regardless of
which of the seven domains failed.

`LiveSapBackend.execute` raises `NotImplementedError` on the
red-green path — live S/4HANA OData transport lands behind the
nightly `-m live` mark per Decision Q3 (real S/4HANA preprod tenant
with read-only creds preferred; SAP Cloud trial tenant is the
documented fallback if Basis access is delayed).

No fixtures committed yet — the recorded backend is still the
sandbox StubGateway set in `api/sandbox_gateways.py`. Weekly
`scripts/record_gateway.py` captures land under
`gateways/fixtures/sap/<domain>/` once the SAP Basis team confirms
read-only credentials for the `S4HANA-preprod` tenant
(`docs/ops/fixture-capture.md` row 2).
