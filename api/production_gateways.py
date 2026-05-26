"""Production gateway registration — fail-loud skeleton.

A production boot **must** register real connectors before serving any
request. Until the platform team has wired Microsoft Graph (email_intake),
Azure Document Intelligence (order_extraction), real SAP S/4HANA
(sap_order / sap_doc / sap_contract / sap_block / sap_customer_master),
real OMS, and the rest of the audit-bearing surface, this function
raises ``NotImplementedError`` so the app refuses to start.

The alternative — an empty gateway registry — would let the app boot,
then fail with ``Gateway not registered: <name>`` on the first
``/resolve`` call. Silent until a request lands. Fail-loud is the
right posture here (per the v3 plan, Phase 0b, Security + Azure/SRE
review).

To wire real connectors:
  1. Implement the connector module(s) under `gateways/` (mirroring
     the StubGateway operation contracts in `api/sandbox_gateways.py`).
  2. Replace the body of ``register_production_gateways`` with explicit
     ``register_gateway(...)`` calls per connector.
  3. Each connector must satisfy the recorded-fixture replay test +
     the shadow-mode diff thresholds in
     ``tests/eval/shadow_mode_thresholds.yaml`` before flipping from
     stub to real.

Env discipline: this function is **not** env-gated; the dispatcher
in ``api/app.py`` routes only ``ASOE_ENV=production`` here.
"""
from __future__ import annotations


_MESSAGE = (
    "register_production_gateways is not implemented — the platform team "
    "has not yet wired real connectors. Edit api/production_gateways.py "
    "to register real Microsoft Graph / Azure Document Intelligence / "
    "SAP S/4HANA / OMS connectors before booting with ASOE_ENV=production. "
    "Audit-bearing fields that will NOT populate until this is done: "
    "email_source_context (from email_intake.fetch_message), "
    "inbox_entities + order_entry_extraction (from order_extraction), "
    "sap_data (from sap_order.validate), "
    "matched_po_details (from oms.get_matched_po_details), "
    "inventory_snapshot (from oms.get_inventory_snapshot), "
    "sap_doc_context / contract_context / promotion_context / "
    "block_context / customer_master_context (from the sap_* gateways), "
    "sla_contract_context (from sla_contract.lookup). "
    "Until then, ASOE_ENV=preprod (in api/preprod_gateways.py) is the "
    "safe non-sandbox boot."
)


def register_production_gateways() -> None:
    """Fail-loud production registration — raises until real connectors
    are wired (see module docstring)."""
    raise NotImplementedError(_MESSAGE)
