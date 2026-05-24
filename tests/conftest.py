from __future__ import annotations

import os

import pytest
from contracts.models import GatewayResponse, GraphState, OrderEvent
from gateways.registry import clear_registry, register_gateway
from gateways.stub import StubGateway
from gateways.tenant_config import TenantConfigGateway


@pytest.fixture(autouse=True)
def _asoe_env_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `ASOE_ENV=sandbox` for the test session unless an explicit
    test scenario overrides it (e.g. tests/test_security.py and
    tests/test_sandbox_routes.py both monkeypatch ASOE_ENV=production
    in specific cases).

    Production became the default-without-env-var when the safe-default
    flip landed (see api/app.py + api/sandbox_gateways.py +
    api/deps.py + api/routes/sandbox.py): a missing ASOE_ENV used to
    silently boot in sandbox mode, which is the wrong default for
    production deploys but the right default for tests. The previous
    behaviour leaked test code into production paths because the
    sandbox-mode CORS allowlist + sandbox routes + stub gateway
    registration all came along for free. New default: production
    (locked-down). Tests opt back into sandbox via this fixture so
    StubGateways register, sandbox routes mount, and CORS allows the
    Playwright origin.
    """
    if not os.getenv("ASOE_ENV"):
        monkeypatch.setenv("ASOE_ENV", "sandbox")


@pytest.fixture
def pricing_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-1001",
            line_item=1,
            po_price=90.0,
            sap_base_price=100.0,
            retailer_id="R-01",
            line_count=1,
        )
    )


@pytest.fixture
def credit_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-2001",
            line_item=1,
            po_price=100.0,
            sap_base_price=100.0,
            requester_role="ORDER_MANAGER",
            credit_limit=10_000.0,
            current_exposure=10_100.0,
            line_count=1,
        )
    )


@pytest.fixture
def mass_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="SO-3001",
            line_item=1,
            po_price=70.0,
            sap_base_price=100.0,
            line_count=11,
        )
    )


@pytest.fixture(autouse=True)
def _register_oms_stub():
    """Register an OMS stub gateway for DuplicatePO resolution context.

    The DuplicatePORecipe declares gateway dependencies (oms/get_fulfillment_status,
    oms/get_matched_po_details) that are resolved by the resolve_dependencies node.
    This stub provides sensible defaults so end-to-end graph tests work without
    real OMS connectivity.
    """
    oms_stub = StubGateway(
        "oms",
        responses={
            "get_fulfillment_status": GatewayResponse(
                gateway_name="oms",
                operation="get_fulfillment_status",
                status="SUCCESS",
                data={"fulfilled": False},
            ),
            "get_inventory_snapshot": GatewayResponse(
                gateway_name="oms",
                operation="get_inventory_snapshot",
                status="SUCCESS",
                # Verdict Pillar 1 (T3): payload carries every
                # audit-bearing subfield required by
                # BackOrderAnalysisData (registry: primary_dc + atp_date
                # always, conditional alternate_warehouses /
                # substitutes / production / inbound_po).
                data={
                    "primary_dc": {
                        "plant": "DC-EAST", "name": "East DC",
                        "region": "US-EAST", "qty": 50.0,
                    },
                    "atp_date": "2026-04-30",
                    "alternate_warehouses": [
                        {
                            "plant": "DC-WEST", "name": "West DC",
                            "region": "US-WEST", "qty": 200.0,
                            "eta_days": 4, "freight_delta_per_unit": 0.50,
                            "freight_delta_total": 25.0,
                        },
                    ],
                    "substitutes": [
                        {
                            "sku": "SKU-BO-1-ALT", "description": "Equivalent SKU",
                            "available_qty": 150.0, "price_delta_pct": 0.02,
                            "acceptance_rate": 0.85, "source": "catalog",
                            "priority": 1,
                        },
                    ],
                    "production": {"qty": 100.0, "date": "2026-05-05"},
                    "inbound_po": {"qty": 75.0, "eta": "2026-05-02", "po_num": "PO-INB-1"},
                },
            ),
            "get_matched_po_details": GatewayResponse(
                gateway_name="oms",
                operation="get_matched_po_details",
                status="SUCCESS",
                # Verdict Pillar 1 (T2): payload carries every
                # audit-bearing subfield required by
                # DuplicateDetectionData (registry: original_order +
                # duplicate_order OrderSnapshot pair, days_between,
                # cancellation_target). Recipe-input booleans
                # (has_revision_indicator, line_items_identical) are
                # alongside.
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
                        "so_number": "SO-DUP-001",
                        "po_number": "PO-4000",
                        "created_date": "2026-04-20",
                        "total_value": 1000.0,
                        "line_count": 1,
                        "status": "OPEN",
                        "lines": [
                            {"sku": "SKU-A", "description": "Widget", "qty": 10.0, "unit_price": 100.0},
                        ],
                    },
                    "duplicate_order": {
                        "so_number": "SO-DUP-002",
                        "po_number": "PO-4001",
                        "created_date": "2026-04-21",
                        "total_value": 1000.0,
                        "line_count": 1,
                        "status": "OPEN",
                        "lines": [
                            {"sku": "SKU-A", "description": "Widget", "qty": 10.0, "unit_price": 100.0},
                        ],
                    },
                },
            ),
        },
    )
    notification_stub = StubGateway(
        "buyer_notification",
        responses={
            "send": GatewayResponse(
                gateway_name="buyer_notification",
                operation="send",
                status="SUCCESS",
                data={"delivered": True},
            ),
        },
    )
    # Verdict T4: SAP doc / contract / promotion stubs supply the
    # audit-bearing PriceAnalysisData fields that retired the
    # price_analysis_gateway_gap clause. Real SAP integration is a
    # separate platform track; these stubs mirror the response shape
    # documented in api/analysis_adapters.py::adapt_price.
    sap_doc_stub = StubGateway(
        "sap_doc",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_doc",
                operation="lookup",
                status="SUCCESS",
                data={
                    "doc_type": "Sales Order",
                    "doc_number": "5500001234",
                    "applied_condition_chain": ["PR00", "K007"],
                    "sku": "SKU-STUB-1",
                    "uom": "CS",
                    "material_desc": "Stub Material",
                    "order_date": "2026-04-22",
                },
            ),
        },
    )
    sap_contract_stub = StubGateway(
        "sap_contract",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_contract",
                operation="lookup",
                status="SUCCESS",
                data={
                    "contract_ref": "KONA-CN-1001",
                    "rule_id": "SO-PRICE-001",
                    "root_cause_category": "CONTRACT_PRICE_OVERRIDE",
                },
            ),
        },
    )
    promotion_stub = StubGateway(
        "promotion",
        responses={
            "lookup": GatewayResponse(
                gateway_name="promotion",
                operation="lookup",
                status="SUCCESS",
                data={
                    "promotion_ref": "PRMO-2026-04-Q2",
                    "root_cause_category": "PROMOTION_HONOR",
                },
            ),
        },
    )
    # Verdict T5: SAP block + customer-master + SLA contract stubs
    # supply the audit-bearing fields that retired the
    # delivery_delay_financial_gap / overmax_gateway_gap /
    # moq_gateway_gap clauses. Real SAP integration is a separate
    # platform track; these stubs mirror the response shape
    # documented in api/analysis_adapters.py.
    sap_block_stub = StubGateway(
        "sap_block",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_block",
                operation="lookup",
                status="SUCCESS",
                data={
                    "block_status": "ACTIVE",
                    "block_reason": "OVER_MAX_QTY",
                    "block_message": "Order exceeds contractual maximum",
                },
            ),
        },
    )
    sap_customer_master_stub = StubGateway(
        "sap_customer_master",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sap_customer_master",
                operation="lookup",
                status="SUCCESS",
                data={
                    "moq_source": "KNMT-MINBM",
                    "channel": "DIRECT",
                },
            ),
        },
    )
    sla_contract_stub = StubGateway(
        "sla_contract",
        responses={
            "lookup": GatewayResponse(
                gateway_name="sla_contract",
                operation="lookup",
                status="SUCCESS",
                data={
                    "sla_deadline": "2026-04-25T00:00:00Z",
                    "at_risk": 1500.0,
                },
            ),
        },
    )
    # ADR-034 Phase B: email_intake gateway carries the four
    # non-disable-able floor checks for EmailOrderEntryRecipe.
    # All four operations are required_for_audit=True; their results
    # populate enrichment_context.{sender_auth_context,
    # customer_resolution_context, duplicate_po_pre_check_context,
    # credit_check_context} which `adapt_email_order_entry` projects
    # into `EmailOrderEntryAnalysisData.floor_status`.
    email_intake_stub = StubGateway(
        "email_intake",
        responses={
            "sender_auth": GatewayResponse(
                gateway_name="email_intake",
                operation="sender_auth",
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
                gateway_name="email_intake",
                operation="resolve_customer",
                status="SUCCESS",
                data={
                    "customer_resolved": True,
                    "match_method": "domain",
                    "match_confidence": 0.97,
                    "customer_name": "Stub Customer Inc",
                },
            ),
            "duplicate_po_pre_check": GatewayResponse(
                gateway_name="email_intake",
                operation="duplicate_po_pre_check",
                status="SUCCESS",
                data={
                    "duplicate_po_clear": True,
                    "matched_po_id": None,
                    "match_score": 0.0,
                },
            ),
            "credit_check": GatewayResponse(
                gateway_name="email_intake",
                operation="credit_check",
                status="SUCCESS",
                data={
                    "credit_clear": True,
                    "credit_limit": 100_000.0,
                    "current_exposure": 25_000.0,
                    "headroom": 75_000.0,
                },
            ),
            # ADR-034 Phase G — fetch_message supplies the email
            # source-of-truth substrate the EmailSourceSection renders.
            "fetch_message": GatewayResponse(
                gateway_name="email_intake",
                operation="fetch_message",
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
    )
    # ADR-042 Phase 3 — Customer Inbox read producers. Deterministic stubs for
    # the constrained-generation order-extraction read + the SAP "validate"
    # read; their output populates enrichment_context.{order_entry_extraction,
    # inbox_entities, sap_data}, activating the Order Entry / Entities / SAP
    # Data tabs. Mirrors api/sandbox_gateways.py so the live sandbox server and
    # the pytest pipeline behave identically.
    order_extraction_stub = StubGateway(
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
    )
    sap_order_stub = StubGateway(
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
    )
    # ADR-042 Phase 3 — ERP write target for SubmitToErpRecipe's effect.
    erp_stub = StubGateway(
        "erp",
        responses={
            "create_sales_order": GatewayResponse(
                gateway_name="erp", operation="create_sales_order",
                status="SUCCESS",
                data={"sap_doc_number": "5100099999", "created": True},
            ),
        },
    )
    register_gateway(oms_stub)
    register_gateway(notification_stub)
    register_gateway(sap_doc_stub)
    register_gateway(sap_contract_stub)
    register_gateway(promotion_stub)
    register_gateway(sap_block_stub)
    register_gateway(sap_customer_master_stub)
    register_gateway(sla_contract_stub)
    register_gateway(email_intake_stub)
    register_gateway(order_extraction_stub)
    register_gateway(sap_order_stub)
    register_gateway(erp_stub)
    # ADR-029: tenant_config is registered as the real file-backed
    # gateway (not a stub) — it's pure in-process I/O against
    # gateways/configs/duplicate_po/defaults.json, so graph tests
    # exercise the actual resolver path. Dedicated unit tests for the
    # gateway itself live in tests/test_tenant_config_gateway.py.
    register_gateway(TenantConfigGateway())
    yield
    clear_registry()


@pytest.fixture
def duplicate_po_event() -> GraphState:
    return GraphState(
        event=OrderEvent(
            order_id="PO-4001",
            line_item=1,
            po_price=100.0,
            sap_base_price=100.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-10",
            line_count=1,
            metadata={
                "signal_scores": {
                    "po_number": 1.0,
                    "customer_id": 1.0,
                    "line_items": 0.95,
                    "amount": 0.90,
                    "timestamp": 0.80,
                    "ship_to": 0.80,
                    "channel": 1.0,
                    "delivery_date": 0.80,
                },
                "matched_po_id": "PO-4000",
            },
        )
    )


@pytest.fixture
def duplicate_po_resend_event() -> GraphState:
    """EC04: exact PO resend (Costco-style) — high signals, timestamp lower."""
    return GraphState(
        event=OrderEvent(
            order_id="PO-88424",
            line_item=1,
            po_price=36.0,
            sap_base_price=36.0,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-05",
            line_count=2,
            metadata={
                "signal_scores": {
                    "po_number": 1.0,
                    "customer_id": 1.0,
                    "line_items": 1.0,
                    "amount": 1.0,
                    "timestamp": 0.3,
                    "ship_to": 1.0,
                    "channel": 1.0,
                    "delivery_date": 1.0,
                },
                # ADR-028 G1: matched_po_id is required by the
                # DUPLICATE_PO event-metadata contract — every event the
                # upstream classifier routes to DUPLICATE_PO must carry
                # the candidate it scored against. EC04 is a resend of
                # PO-88424 — the candidate is the originally-transmitted
                # PO of the same number.
                "matched_po_id": "PO-88424-original",
            },
        )
    )


@pytest.fixture
def duplicate_po_batch_event() -> GraphState:
    """EC08: one PO from a multi-PO batch (Amazon-style) — no match → PASS.

    Even when the recipe's composite_score will be low (no real match),
    the upstream classifier still routes DUPLICATE_PO because it
    *attempted* a candidate match against PO-AMZ-CANDIDATE — the recipe
    then scores 0 across signals and correctly emits PASS.
    """
    return GraphState(
        event=OrderEvent(
            order_id="PO-AMZ-003",
            line_item=1,
            po_price=8.96,
            sap_base_price=8.96,
            event_type="EDI_850_DUPLICATE_PO",
            retailer_id="R-08",
            line_count=1,
            metadata={
                "signal_scores": {
                    "po_number": 0.0,
                    "customer_id": 1.0,
                    "line_items": 0.0,
                    "amount": 0.0,
                    "timestamp": 0.0,
                    "ship_to": 0.0,
                    "channel": 1.0,
                    "delivery_date": 0.0,
                },
                "matched_po_id": "PO-AMZ-CANDIDATE",
                "source_email_id": "EC08-multiple-pos-amazon",
                "batch_po_index": 3,
            },
        )
    )
