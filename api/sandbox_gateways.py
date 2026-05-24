"""Sandbox-only StubGateway registration.

The live FastAPI server has no real SAP / OMS / SLA-contract
integrations wired (those are platform-team work, separate from this
repo). For local end-to-end runs we register the same stubs that
`tests/conftest.py` uses so the recipes' GatewayDependency entries
resolve and the audit-bearing fields populate downstream.

Mounted only when ASOE_ENV=sandbox (the default). Idempotent —
clear_registry() is called first so re-running the app on a hot
reload starts clean.

Real-environment behavior is identical: the recipes declare what they
need; whoever runs the server is responsible for wiring the real
gateway adapters at startup. This module is the sandbox shim.
"""

from __future__ import annotations

import os

from contracts.models import GatewayResponse
from db.repository import TenantConfigRepository as _TenantConfigRepository
from db.shared import get_shared_adapter
from gateways.edi850 import build_edi_850
from gateways.knowledge_graph import build_knowledge_graph
from gateways.registry import clear_registry, register_gateway
from gateways.stub import StubGateway
from gateways.tenant_config import TenantConfigGateway
from contracts.policy import HIGH_VALUE_OVERRIDE_THRESHOLD_USD
from recipes.ChangeAnalysisRecipe import evaluate_change

# ADR-042 Phase 5 — the canned order the edi_850 builder stub reconstructs.
# Mirrors the order_extraction stub (same PO / customer / line) so the EDI 850
# Audit tab is consistent with the Order Entry tab. The seller is the ASOE
# tenant identity (not fabricated third-party data). Kept in sync with the
# identical constant in tests/conftest.py.
_SANDBOX_EDI_850_ORDER = dict(
    order_id="0093847612",
    po_number="0093847612",
    po_date="2025-03-17",
    buyer_name="Walmart Stores Inc",
    buyer_id="300001",
    seller_name="Acme Beverages Co",
    seller_id="VENDOR-7788",
    requested_date="2025-03-24",
    line_items=[{
        "line_num": "001", "material": "BEV-COLA-12PK",
        "description": "Cola 12-pack case", "quantity": 480,
        "uom": "CS", "unit_price": 8.64,
    }],
)

# ADR-042 Phase 6 — the canned order-change the change_analysis evaluator scores.
# A qty-increase change with a representative mix of constraint signals; kept in
# sync with the identical constant in tests/conftest.py.
_SANDBOX_CHANGE = dict(
    order_id="0093847612",
    order_value_usd=45200.0,
    cosign_threshold_usd=HIGH_VALUE_OVERRIDE_THRESHOLD_USD,
    lifecycle_index=2,
    change_items=[
        {"field": "quantity", "from_value": "480", "to_value": "600"},
        {"field": "requested_date", "from_value": "2025-03-24", "to_value": "2025-03-20"},
    ],
    signals={
        "inventory": {"atp": 520, "required": 600},
        "production": {"stage": "REL"},
        "transport": {"route_available": True, "carrier_capacity": True},
        "warehouse": {"pick_pack_feasible": True},
        "order_status": {"fulfillment_stage": 2},
        "sla": {"within_window": True, "days_to_deadline": 1},
        "dependencies": {"linked_orders": 1},
        "network": {"dc_routing_ok": True},
        "priority": {"customer_tier": "GOLD", "auto_approve": False},
    },
)

# ADR-042 Phase 7 — the canned entities the knowledge_graph builder projects.
# Derived from the same order/customer/SAP context the other producers return.
_SANDBOX_KG = dict(
    order_id="0093847612",
    customer_name="Walmart Stores Inc",
    customer_bp="300001",
    line_items=[
        {"material": "BEV-COLA-12PK", "description": "Cola 12-pack case",
         "quantity": 480, "uom": "CS"},
    ],
    sap={"system": "S4H_PRD", "sap_doc_number": "5100012344",
         "validation_status": "SO confirmed, ATP OK"},
    entities=[
        {"key": "customer_po", "value": "0093847612", "kind": "po"},
    ],
)


def register_sandbox_gateways() -> None:
    """Register every stub gateway needed for end-to-end runs.

    Mirrors tests/conftest.py so live-server runs match the test
    pipeline. Safe to call multiple times — clears the registry
    first.
    """
    if os.getenv("ASOE_ENV", "production").lower() != "sandbox":
        return

    clear_registry()

    register_gateway(StubGateway(
        "oms",
        responses={
            "get_fulfillment_status": GatewayResponse(
                gateway_name="oms", operation="get_fulfillment_status",
                status="SUCCESS", data={"fulfilled": False},
            ),
            "get_inventory_snapshot": GatewayResponse(
                gateway_name="oms", operation="get_inventory_snapshot",
                status="SUCCESS",
                data={
                    "primary_dc": {
                        "plant": "DC-EAST", "name": "East DC",
                        "region": "US-EAST", "qty": 50.0,
                    },
                    "atp_date": "2026-04-30",
                    "alternate_warehouses": [{
                        "plant": "DC-WEST", "name": "West DC",
                        "region": "US-WEST", "qty": 200.0,
                        "eta_days": 4, "freight_delta_per_unit": 0.50,
                        "freight_delta_total": 25.0,
                    }],
                    "substitutes": [{
                        "sku": "SKU-BO-1-ALT", "description": "Equivalent SKU",
                        "available_qty": 150.0, "price_delta_pct": 0.02,
                        "acceptance_rate": 0.85, "source": "catalog",
                        "priority": 1,
                    }],
                    "production": {"qty": 100.0, "date": "2026-05-05"},
                    "inbound_po": {"qty": 75.0, "eta": "2026-05-02", "po_num": "PO-INB-1"},
                },
            ),
            "get_matched_po_details": GatewayResponse(
                gateway_name="oms", operation="get_matched_po_details",
                status="SUCCESS",
                data={
                    "has_revision_indicator": False,
                    "line_items_identical": True,
                    "days_between": 1,
                    "cancellation_target": "SO-DUP-002",
                    "detection_method": "po_number+customer+lines",
                    "customer_id": "R-10",
                    "matching_fields": ["po_number", "customer_id", "line_items"],
                    "differing_fields": [],
                    "original_order": {
                        "so_number": "SO-DUP-001", "po_number": "PO-4000",
                        "created_date": "2026-04-20", "total_value": 1000.0,
                        "line_count": 1, "status": "OPEN",
                        "lines": [{"sku": "SKU-A", "description": "Widget",
                                   "qty": 10.0, "unit_price": 100.0}],
                    },
                    "duplicate_order": {
                        "so_number": "SO-DUP-002", "po_number": "PO-4001",
                        "created_date": "2026-04-21", "total_value": 1000.0,
                        "line_count": 1, "status": "OPEN",
                        "lines": [{"sku": "SKU-A", "description": "Widget",
                                   "qty": 10.0, "unit_price": 100.0}],
                    },
                },
            ),
            "get_price_hold_status": GatewayResponse(
                gateway_name="oms", operation="get_price_hold_status",
                status="SUCCESS", data={"status": "HELD"},
            ),
        },
    ))

    register_gateway(StubGateway(
        "buyer_notification",
        responses={
            "send": GatewayResponse(
                gateway_name="buyer_notification", operation="send",
                status="SUCCESS", data={"delivered": True},
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_doc",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_doc", operation="lookup",
                status="SUCCESS",
                data={
                    "doc_type": "Sales Order", "doc_number": "5500001234",
                    "applied_condition_chain": ["PR00", "K007"],
                    "sku": "SKU-STUB-1", "uom": "CS",
                    "material_desc": "Stub Material",
                    "order_date": "2026-04-22",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_contract",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_contract", operation="lookup",
                status="SUCCESS",
                data={
                    "contract_ref": "KONA-CN-1001",
                    "rule_id": "SO-PRICE-001",
                    "root_cause_category": "CONTRACT_PRICE_OVERRIDE",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "promotion",
        responses={
            "lookup": GatewayResponse(
                gateway_name="promotion", operation="lookup",
                status="SUCCESS",
                data={
                    "promotion_ref": "PRMO-2026-04-Q2",
                    "root_cause_category": "PROMOTION_HONOR",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_block",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_block", operation="lookup",
                status="SUCCESS",
                data={
                    "block_status": "ACTIVE",
                    "block_reason": "OVER_MAX_QTY",
                    "block_message": "Order exceeds contractual maximum",
                },
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_customer_master",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_customer_master", operation="lookup",
                status="SUCCESS",
                data={"moq_source": "KNMT-MINBM", "channel": "DIRECT"},
            ),
        },
    ))

    register_gateway(StubGateway(
        "sla_contract",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sla_contract", operation="lookup",
                status="SUCCESS",
                data={
                    "sla_deadline": "2026-04-25T00:00:00Z",
                    "at_risk": 1500.0,
                },
            ),
        },
    ))

    # ADR-034 Phase B — `email_intake` carries the four non-disable-able
    # floor checks for EmailOrderEntryRecipe. Mirrors
    # tests/conftest.py::email_intake_stub so the live sandbox server
    # behaves identically to the pytest pipeline. Real upstream connectors
    # (Microsoft Graph / customer-master / OMS / credit service) ship as a
    # platform-track follow-up under proposed ADR-035.
    register_gateway(StubGateway(
        "email_intake",
        responses={
            "sender_auth": GatewayResponse(
                gateway_name="email_intake", operation="sender_auth",
                status="SUCCESS",
                data={
                    "sender_authorized": True,
                    "auth_method": "domain_match",
                    "auth_evidence": {
                        "from_domain": "stub-customer.example",
                        "matched_against": "customer_master.allowed_domains",
                    },
                },
            ),
            "resolve_customer": GatewayResponse(
                gateway_name="email_intake", operation="resolve_customer",
                status="SUCCESS",
                data={
                    "customer_resolved": True,
                    "match_method": "domain",
                    "match_confidence": 0.97,
                    "customer_name": "Stub Customer Inc",
                },
            ),
            "duplicate_po_pre_check": GatewayResponse(
                gateway_name="email_intake", operation="duplicate_po_pre_check",
                status="SUCCESS",
                data={
                    "duplicate_po_clear": True,
                    "matched_po_id": None,
                    "match_score": 0.0,
                },
            ),
            "credit_check": GatewayResponse(
                gateway_name="email_intake", operation="credit_check",
                status="SUCCESS",
                data={
                    "credit_clear": True,
                    "credit_limit": 100_000.0,
                    "current_exposure": 25_000.0,
                    "headroom": 75_000.0,
                },
            ),
            "fetch_message": GatewayResponse(
                gateway_name="email_intake", operation="fetch_message",
                status="SUCCESS",
                data={
                    "from_address": "buyer@stub-customer.example",
                    "received_at": "2026-04-30T10:12:00Z",
                    "subject": "PO submission — stub fixture",
                    "body_hash": (
                        "0000000000000000000000000000000000000000"
                        "000000000000000000000000"
                    ),
                    "attachment_manifest": [
                        {
                            "name": "purchase_order.pdf",
                            "mime_type": "application/pdf",
                            "bytes": 12_345,
                        },
                    ],
                    "body_excerpt": (
                        "Please process the attached PO. "
                        "Ship to the Atlanta DC."
                    ),
                    "source_email_id": "stub-msg-001",
                },
            ),
        },
    ))

    # ADR-042 Phase 3 — Customer Inbox read producers. The order-extraction
    # gateway is the constrained-generation read (here a deterministic stub
    # standing in for the live model, mirroring tests/conftest.py); sap_order
    # is the deterministic SAP "validate" read. Their output activates the
    # Order Entry / Entities / SAP Data tabs via enrichment_context.
    register_gateway(StubGateway(
        "order_extraction",
        responses={
            "extract_order": GatewayResponse(
                gateway_name="order_extraction", operation="extract_order",
                status="SUCCESS",
                data={
                    "source_type": "PDF",
                    "confidence": 0.94,
                    "header": {
                        "customer_po": "0093847612", "order_type": "ZOR",
                        "sales_org": "1000", "dist_channel": "10",
                        "requested_date": "2025-03-17",
                    },
                    "customer_name": "Walmart Stores Inc",
                    "customer_bp": "300001",
                    "line_items": [{
                        "line_num": "001", "material": "BEV-COLA-12PK",
                        "description": "Cola 12-pack case", "quantity": 480,
                        "uom": "CS", "unit_price": 8.64, "mdm_matched": True,
                    }],
                    "validation_flags": [{
                        "field": "line 001", "severity": "INFO",
                        "message": "matched to material master",
                    }],
                },
            ),
            "extract_entities": GatewayResponse(
                gateway_name="order_extraction", operation="extract_entities",
                status="SUCCESS",
                data={"extracted": [
                    {"key": "customer_po", "value": "0093847612", "kind": "po",
                     "confidence": 0.98, "source_span": "PO# 0093847612"},
                    {"key": "material", "value": "BEV-COLA-12PK",
                     "kind": "material", "confidence": 0.95,
                     "source_span": "Cola 12pk case"},
                ]},
            ),
        },
    ))

    register_gateway(StubGateway(
        "sap_order",
        responses={
            "validate": GatewayResponse(
                gateway_name="sap_order", operation="validate",
                status="SUCCESS",
                data={
                    "system": "S4H_PRD",
                    "validation_status": "SO confirmed, ATP OK",
                    "order_value_usd": 45200.0,
                    "sap_doc_number": "5100012344",
                },
            ),
        },
    ))

    # ADR-042 Phase 5 — EDI 850 builder producer. Deterministic X12 850
    # reconstruction of the same canned order the order_extraction stub
    # returns; activates the EDI 850 Audit tab via enrichment_context.edi_850.
    register_gateway(StubGateway(
        "edi_850",
        responses={
            "build": GatewayResponse(
                gateway_name="edi_850", operation="build",
                status="SUCCESS",
                data=build_edi_850(**_SANDBOX_EDI_850_ORDER),
            ),
        },
    ))

    # ADR-042 Phase 6 — Change Analysis evaluator producer. Deterministic
    # constraint evaluation of the canned order change; activates the Change
    # Analysis tab via enrichment_context.change_analysis.
    register_gateway(StubGateway(
        "change_analysis",
        responses={
            "evaluate": GatewayResponse(
                gateway_name="change_analysis", operation="evaluate",
                status="SUCCESS",
                data=evaluate_change(**_SANDBOX_CHANGE),
            ),
        },
    ))

    # ADR-042 Phase 7 — Knowledge Graph builder producer. Deterministic derived
    # projection of the canned case entities; activates the Knowledge Graph tab
    # via enrichment_context.knowledge_graph.
    register_gateway(StubGateway(
        "knowledge_graph",
        responses={
            "build": GatewayResponse(
                gateway_name="knowledge_graph", operation="build",
                status="SUCCESS",
                data=build_knowledge_graph(**_SANDBOX_KG),
            ),
        },
    ))

    # ADR-042 Phase 3 — the ERP write target for SubmitToErpRecipe's effect
    # (sales-order create). Stub stands in for the real BAPI/EDI-850 connector.
    register_gateway(StubGateway(
        "erp",
        responses={
            "create_sales_order": GatewayResponse(
                gateway_name="erp", operation="create_sales_order",
                status="SUCCESS",
                data={"sap_doc_number": "5100099999", "created": True},
            ),
        },
    ))

    # ADR-029 / ADR-030: tenant_config is the file-backed PLATFORM resolver
    # for layer 1 + the DB-backed resolver for layers 2-5 (PR-C.2). It's
    # not a stub — the platform JSON ships with the application and the
    # DB-backed layers come from tenant_config table via the shared
    # adapter (db.shared.get_shared_adapter). Without this registration,
    # resolve_dependencies fails with "Gateway not registered:
    # tenant_config" on every DUPLICATE_PO event, halting the graph at
    # FAIL_TO_HUMAN before the recipe runs. Mirrors
    # tests/conftest.py::_register_oms_stub for the pytest pipeline.
    register_gateway(TenantConfigGateway(
        repository=_TenantConfigRepository(adapter=get_shared_adapter()),
    ))
