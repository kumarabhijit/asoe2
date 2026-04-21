"""ASOE Sandbox — Streamlit execution trace visualiser.

Supports two execution modes (matching sandbox CLI):
  1. **Direct mode** (default): Calls ``run_graph()`` directly.
  2. **API mode**: Authenticates via ``/api/auth/login`` multi-step flow,
     then uses the 19 REST endpoints from architecture_v3.md Section 8.2.

Additional panels (matching sandbox CLI test cases):
  - Auth flow validation (multi-step login, SSO, token refresh)
  - Compliance Shadow simulation (force BLOCKED / MANUAL_REVIEW_REQUIRED)
  - DB persistence verification (exception + trace storage)
  - WebSocket event monitor (pub/sub events after resolve)
  - Dashboard stats (aggregate metrics from /api/v1/exceptions/stats)
  - Lily personality toggle for conversational output

Launch
------
    cd /path/to/asoe
    PYTHONPATH=. streamlit run tests/sandbox/ui/app.py

Optional env vars
-----------------
    SANDBOX_DB_PATH           Path to sandbox.db (default: tests/sandbox/sandbox.db)
    ASOE_API_BASE_URL         Base URL for API mode (default: TestClient in-process)
    LOCAL_LLM_BACKEND_CLASS   e.g. tests.sandbox.llm.local_backend.LocalHFBackend
    LOCAL_LLM_MODEL           HuggingFace model id (default: Qwen/Qwen2.5-0.5B-Instruct)
    ASOE_EXPLAIN_MODE         1 = dry-run mode (no recipe side effects)
    ASOE_KILL_SWITCH          1 = kill switch active (halts all execution)
    LANGFUSE_PUBLIC_KEY       LangFuse public key (enables trace forwarding)
    LANGFUSE_SECRET_KEY       LangFuse secret key (required alongside public key)
    LANGFUSE_HOST             LangFuse host URL (omit for LangFuse Cloud)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------
# Ensure repo root is on sys.path regardless of how streamlit is run.
# ------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

# ---- ASOE imports (after path fix) --------------------------------
from contracts.models import GatewayResponse, GraphState, Intent, OrderEvent
from gateways.registry import register_gateway, clear_registry
from gateways.stub import StubGateway
from orchestration.graph import run_graph
from tests.sandbox.seed import (
    load_events,
    load_customers,
    load_promotions,
    lookup_customer,
    lookup_credit_profile,
    DB_DEFAULT,
)

# ---- API / Auth imports (for API mode) --------------------------------
try:
    from fastapi.testclient import TestClient
    from api.app import create_app
    from api.deps import create_test_token, create_refresh_token
    from api.events import WSEvent
    from api.pubsub import InMemoryPubSub, event_publisher
    from api.store import exception_store
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


def _register_sandbox_gateways() -> None:
    """Register stub gateways for DuplicatePO resolution context."""
    clear_registry()
    oms_stub = StubGateway(
        "oms",
        responses={
            "get_fulfillment_status": GatewayResponse(
                gateway_name="oms",
                operation="get_fulfillment_status",
                status="SUCCESS",
                data={"fulfilled": False},
            ),
            "get_matched_po_details": GatewayResponse(
                gateway_name="oms",
                operation="get_matched_po_details",
                status="SUCCESS",
                data={
                    "has_revision_indicator": False,
                    "line_items_identical": True,
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
    register_gateway(oms_stub)
    register_gateway(notification_stub)

# ------------------------------------------------------------------
# API Client (architecture_v3.md Section 8.2 — same as sandbox CLI)
# ------------------------------------------------------------------

class SandboxAPIClient:
    """HTTP client for REST endpoints. Uses TestClient in-process."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._client: Any = None
        self._auth_log: List[Dict[str, Any]] = []

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        base_url = os.getenv("ASOE_API_BASE_URL", "")
        if base_url:
            import httpx
            self._client = httpx.Client(base_url=base_url, timeout=30)
        elif _API_AVAILABLE:
            self._client = TestClient(create_app(), raise_server_exceptions=False)
        else:
            raise RuntimeError("FastAPI not available. Install fastapi + uvicorn.")
        return self._client

    def _headers(self) -> Dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    @property
    def auth_log(self) -> List[Dict[str, Any]]:
        return self._auth_log

    @property
    def token(self) -> Optional[str]:
        return self._token

    def authenticate(self, email: str = "admin@asoe.test",
                     password: str = "test-password") -> bool:
        """Multi-step auth: login -> MFA -> access_token."""
        self._auth_log = []
        client = self._get_client()

        # Step 1: Login
        resp = client.post("/api/auth/login",
                           json={"email": email, "password": password})
        self._auth_log.append({
            "step": "1. Login", "endpoint": "POST /api/auth/login",
            "status": resp.status_code,
            "mfa_required": resp.json().get("mfa_required", False),
            "result": "MFA challenge" if resp.status_code == 200 else "FAILED",
        })
        if resp.status_code != 200:
            return False
        login_data = resp.json()
        if not login_data.get("mfa_required"):
            self._token = login_data.get("access_token")
            return bool(self._token)

        # Step 2: MFA
        mfa_resp = client.post("/api/auth/mfa/verify",
                               json={"mfa_token": login_data["mfa_token"],
                                     "code": "123456"})
        self._auth_log.append({
            "step": "2. MFA Verify", "endpoint": "POST /api/auth/mfa/verify",
            "status": mfa_resp.status_code,
            "has_token": bool(mfa_resp.json().get("access_token")),
            "result": "JWT issued" if mfa_resp.status_code == 200 else "FAILED",
        })
        if mfa_resp.status_code != 200:
            return False
        self._token = mfa_resp.json().get("access_token")
        return bool(self._token)

    def resolve(self, event_data: Dict[str, Any],
                explain: bool = False) -> Dict[str, Any]:
        endpoint = "/api/v1/exceptions/resolve"
        if explain:
            endpoint += "/explain"
        resp = self._get_client().post(endpoint, json=event_data,
                                       headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"Resolve failed ({resp.status_code}): {resp.json()}")
        return resp.json()

    def get_exception(self, exception_id: str) -> Dict[str, Any]:
        resp = self._get_client().get(f"/api/v1/exceptions/{exception_id}",
                                      headers=self._headers())
        return resp.json()

    def get_trace(self, exception_id: str) -> Dict[str, Any]:
        resp = self._get_client().get(f"/api/v1/exceptions/{exception_id}/trace",
                                      headers=self._headers())
        return resp.json()

    def get_stats(self) -> Dict[str, Any]:
        resp = self._get_client().get("/api/v1/exceptions/stats",
                                      headers=self._headers())
        return resp.json()

    def list_exceptions(self, limit: int = 20) -> Dict[str, Any]:
        resp = self._get_client().get(f"/api/v1/exceptions?limit={limit}",
                                      headers=self._headers())
        return resp.json()

    def health(self) -> Dict[str, Any]:
        resp = self._get_client().get("/api/v1/health")
        return resp.json()


# ------------------------------------------------------------------
# Page config
# ------------------------------------------------------------------
st.set_page_config(
    page_title="ASOE Sandbox",
    page_icon="⚙️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_VERDICT_COLOUR = {
    "GREEN":  "🟢",
    "YELLOW": "🟡",
    "RED":    "🔴",
}

_STATUS_COLOUR = {
    "COMPLETE":                "✅",
    "FAIL_TO_HUMAN":           "🚨",
    "MANUAL_REVIEW_REQUIRED":  "🟡",
    "BLOCKED":                 "🔴",
    "REJECTED":                "❌",
}


def _db_path() -> Path:
    custom = os.getenv("SANDBOX_DB_PATH", "")
    return Path(custom) if custom else DB_DEFAULT


def _load_skill_text(intent: str) -> Optional[str]:
    """Return the raw SKILL.md text for the given intent, or None."""
    skills_root = _REPO_ROOT / "skills"
    mapping = {
        "CONTRACTUAL_CORRECTION": "pricing-reconciliation_SKILL.md",
        "CREDIT_BLOCK":           "pricing-reconciliation_SKILL.md",
        "MASS_PRICING_ERROR":     "pricing-reconciliation_SKILL.md",
        "DUPLICATE_PO":           "duplicate-po_SKILL.md",
        "PRICE_HOLD_RELEASE":     "price-hold-release_SKILL.md",
        "EDI_MISMATCH":           "edi-mismatch_SKILL.md",
        "BACK_ORDER":             "back-order-resolution_SKILL.md",
        "OVER_MAX":               "over-max-trim_SKILL.md",
        "MIN_ORDER_QTY":          "moq-round-up_SKILL.md",
        "PALLET_CONFIG":          "pallet-alignment_SKILL.md",
        "DELIVERY_DELAY":         "delivery-delay_SKILL.md",
    }
    filename = mapping.get(intent)
    if not filename:
        return None
    skill_path = skills_root / filename
    if skill_path.exists():
        return skill_path.read_text()
    return None


def _edi_row_to_order_event(row: Dict[str, Any]) -> OrderEvent:
    """Convert a raw DB row dict to an OrderEvent."""
    metadata: Dict[str, Any] = {}
    raw_meta = row.get("metadata", "{}")
    if isinstance(raw_meta, str):
        try:
            metadata = json.loads(raw_meta)
        except Exception:  # noqa: BLE001
            pass
    else:
        metadata = raw_meta

    return OrderEvent(
        order_id=row["order_id"],
        event_type=row["event_type"],
        sku=row.get("sku"),
        po_price=float(row.get("po_price") or 0.0),
        sap_base_price=float(row.get("sap_price") or 0.0),
        retailer_id=row.get("retailer_id"),
        line_count=int(row.get("line_count") or 1),
        requester_role=metadata.get("requester_role"),
        credit_limit=metadata.get("credit_limit"),
        current_exposure=metadata.get("current_exposure"),
        metadata=metadata,
    )


def _intent_label(row: Dict[str, Any]) -> str:
    evt_id = row.get("event_id", "")
    if evt_id.startswith("EVT-CC-"):
        return "CONTRACTUAL_CORRECTION"
    if evt_id.startswith("EVT-CB-"):
        return "CREDIT_BLOCK"
    if evt_id.startswith("EVT-MPE-"):
        return "MASS_PRICING_ERROR"
    if evt_id.startswith("EVT-DPO-"):
        return "DUPLICATE_PO"
    if evt_id.startswith("EVT-PHR-"):
        return "PRICE_HOLD_RELEASE"
    if evt_id.startswith("EVT-EDM-"):
        return "EDI_MISMATCH"
    if evt_id.startswith("EVT-BO-"):
        return "BACK_ORDER"
    if evt_id.startswith("EVT-OM-"):
        return "OVER_MAX"
    if evt_id.startswith("EVT-MOQ-"):
        return "MIN_ORDER_QTY"
    if evt_id.startswith("EVT-PLT-"):
        return "PALLET_CONFIG"
    if evt_id.startswith("EVT-DD-"):
        return "DELIVERY_DELAY"
    return "UNKNOWN"


# ------------------------------------------------------------------
# Sidebar — event picker or custom form
# ------------------------------------------------------------------

def _render_sidebar() -> Optional[OrderEvent]:
    st.sidebar.header("ASOE Sandbox")

    # ── Execution mode ──
    exec_mode = st.sidebar.radio(
        "Execution mode",
        ["Direct (run_graph)", "API (REST endpoints)"],
        index=0,
        help="Direct calls run_graph() in-process. API authenticates via "
             "/api/auth/login and uses the 19 REST endpoints.",
    )
    st.session_state["exec_mode"] = exec_mode

    # ── Simulation controls ──
    st.sidebar.subheader("Simulation controls")
    force_blocked = st.sidebar.checkbox(
        "Force BLOCKED (RED shadow)",
        help="Override line_count to trigger RED Compliance Shadow verdict",
    )
    force_review = st.sidebar.checkbox(
        "Force MANUAL_REVIEW (explain mode)",
        help="Run in explain mode — full pipeline, no recipe execution",
    )
    lily_mode = st.sidebar.checkbox(
        "Lily personality",
        help="Conversational output from the Lily agentic persona",
    )
    st.session_state["force_blocked"] = force_blocked
    st.session_state["force_review"] = force_review
    st.session_state["lily_mode"] = lily_mode

    st.sidebar.divider()

    # ── Event source ──
    db = _db_path()
    use_db = db.exists()

    input_mode = st.sidebar.radio(
        "Event source",
        ["From SQLite (seeded events)", "Custom form"],
        index=0 if use_db else 1,
    )

    if input_mode == "From SQLite (seeded events)":
        if not use_db:
            st.sidebar.error(
                f"sandbox.db not found at `{db}`.\n\n"
                "Run: `python tests/sandbox/seed.py`"
            )
            return None

        events = load_events(db)
        options = {
            f"{r['event_id']} · {r['order_id']} · {_intent_label(r)}": r
            for r in events
        }
        selected_label = st.sidebar.selectbox("Select EDI event", list(options.keys()))
        row = options[selected_label]

        # Show raw DB row
        with st.sidebar.expander("Raw DB row"):
            st.json(row)

        # Show customer + credit context for selected event
        rid = row.get("retailer_id")
        if rid and db.exists():
            cust = lookup_customer(db, rid)
            credit = lookup_credit_profile(db, rid)
            if cust or credit:
                with st.sidebar.expander("Customer context"):
                    if cust:
                        st.write(f"**{cust['name']}** ({cust['tier']})")
                        st.write(f"Region: {cust['region']}")
                    if credit:
                        over = credit["current_exposure"] - credit["credit_limit"]
                        st.write(f"Credit limit: ${credit['credit_limit']:,.0f}")
                        st.write(f"Exposure: ${credit['current_exposure']:,.0f}")
                        if over > 0:
                            st.warning(f"Over limit by ${over:,.0f}")
                        st.write(f"Risk: {credit['risk_rating']}")

        run_clicked = st.sidebar.button("▶  Run event", type="primary")
        if run_clicked:
            return _edi_row_to_order_event(row)
        return None

    # ---- Custom form ----
    st.sidebar.subheader("Custom event")
    with st.sidebar.form("custom_event"):
        order_id    = st.text_input("order_id",       value="SO-9999")
        event_type  = st.selectbox("event_type", [
            "EDI_850_PRICE_MISMATCH",
            "EDI_850_DUPLICATE_PO",
            "EDI_850_PRICE_HOLD",
            "EDI_850_LINE_MISMATCH",
            "BACK_ORDER_OOS",
            "OVER_MAX_QTY",
            "MIN_ORDER_QTY",
            "PALLET_CONFIG_VIOLATION",
            "DELIVERY_DELAY",
        ])
        sku         = st.text_input("sku",            value="SKU-001")
        po_price    = st.number_input("po_price",     value=90.0, step=0.01)
        sap_price   = st.number_input("sap_price",    value=100.0, step=0.01)
        retailer_id = st.text_input("retailer_id",    value="R-01")
        line_count  = st.number_input("line_count",   value=1, min_value=1, step=1)
        # Sub-type chooser for line mismatches; includes PRICE_MISMATCH so
        # the routing fork to CONTRACTUAL_CORRECTION can be demonstrated.
        mismatch_sub_type = st.selectbox("mismatch_sub_type", [
            "(none)",
            "SKU_MISMATCH",
            "QTY_MISMATCH",
            "UOM_MISMATCH",
            "SHIP_TO_MISMATCH",
            "PRICE_MISMATCH",
        ])
        submitted   = st.form_submit_button("▶  Run event", type="primary")

    if submitted:
        metadata: Dict[str, Any] = {}
        if event_type == "EDI_850_DUPLICATE_PO":
            metadata["signal_scores"] = {
                "po_number": 1.0, "customer_id": 1.0, "line_items": 1.0,
                "amount": 1.0, "timestamp": 0.5, "ship_to": 1.0,
                "channel": 1.0, "delivery_date": 1.0,
            }
        elif event_type == "EDI_850_PRICE_HOLD":
            metadata["price_hold_status"] = "HELD"
        elif event_type == "EDI_850_LINE_MISMATCH":
            if mismatch_sub_type and mismatch_sub_type != "(none)":
                metadata["mismatch_sub_type"] = mismatch_sub_type
            metadata.setdefault("expected_value", "expected-x")
            metadata.setdefault("received_value", "received-y")
        elif event_type == "BACK_ORDER_OOS":
            metadata.update({
                "ordered_qty": 100, "available_qty": 75,
                "unit_price": float(po_price), "uom": "CS",
            })
        elif event_type == "OVER_MAX_QTY":
            metadata.update({
                "total_ordered": 130, "max_qty": 100,
                "order_lines": [
                    {"sku": sku, "description": sku, "qty": 130,
                     "max_line_qty": 100, "is_even_layer_item": True},
                ],
            })
        elif event_type == "MIN_ORDER_QTY":
            metadata.update({
                "ordered_qty": 40, "moq_qty": 48,
                "unit_cost": float(po_price), "uom": "CS",
            })
        elif event_type == "PALLET_CONFIG_VIOLATION":
            metadata["pallet_lines"] = [
                {"sku": sku, "description": sku,
                 "layer_qty": 24, "pallet_qty": 96,
                 "ordered_qty": 100, "uom": "CS"},
            ]
        elif event_type == "DELIVERY_DELAY":
            metadata.update({
                "planned_date": "2026-04-20T00:00:00Z",
                "projected_eta": "2026-04-23T00:00:00Z",
                "days_late": 3,
                "delay_category": "CARRIER_DELAY",
            })
        return OrderEvent(
            order_id=order_id,
            event_type=event_type,
            sku=sku,
            po_price=float(po_price),
            sap_base_price=float(sap_price),
            retailer_id=retailer_id,
            line_count=int(line_count),
            metadata=metadata,
        )
    return None


# ------------------------------------------------------------------
# Main trace display
# ------------------------------------------------------------------

def _render_trace(state: GraphState) -> None:
    intent_val  = state.intent.value if state.intent else "UNKNOWN"
    shadow_val  = state.shadow.status.value if state.shadow else "N/A"
    status_val  = state.final_status.value if state.final_status else "N/A"
    recipe_name = state.selected_recipe or "—"

    shadow_icon = _VERDICT_COLOUR.get(shadow_val, "⚪")
    status_icon = _STATUS_COLOUR.get(status_val, "⚪")

    # ---- Header metrics ----
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Intent",         intent_val)
    col2.metric("Shadow Verdict", f"{shadow_icon} {shadow_val}")
    col3.metric("Recipe",         recipe_name)
    col4.metric("Final Status",   f"{status_icon} {status_val}")

    st.divider()

    # ---- Step-by-step execution trace ----
    st.subheader("Execution trace")

    steps = [
        ("1. Ingest",               "✅", f"order_id={state.event.order_id}  "
                                           f"event_type={state.event.event_type}"),
        ("2. Classify",             "✅", f"intent={intent_val}  "
                                           f"confidence={state.confidence:.2f}"),
        ("3. Load Skill",           "✅", f"skill={state.skill.name if state.skill else '—'}"),
        ("4. Circuit Breaker",      "✅", f"batch_variance={state.batch_total_variance:.2f}"),
        ("5. Compliance Shadow",    shadow_icon,
                                        f"verdict={shadow_val}  "
                                        f"policy_hits={state.shadow.policy_hits if state.shadow else []}"),
        ("6. Select Recipe",        "✅" if recipe_name != "—" else "⛔", f"recipe={recipe_name}"),
        ("7. Resolve Dependencies", "✅", f"keys={list(state.resolved_data.keys()) or 'none'}"),
        ("8. Execute Recipe",       status_icon, f"status={status_val}"),
        ("9. Apply Effects",        "✅", f"effects={len(state.effect_results)}"),
    ]

    for label, icon, detail in steps:
        st.markdown(f"**{icon} {label}** — {detail}")

    st.divider()

    # ---- Shadow detail ----
    if state.shadow:
        st.subheader(f"{shadow_icon} Compliance Shadow detail")
        st.write("**Reasons:**", state.shadow.reasons)
        st.write("**Policy hits:**", state.shadow.policy_hits)

    # ---- Explanation ----
    if state.explanation:
        st.subheader("Explanation")
        st.info(state.explanation)

    # ---- Duplicate PO detail (EC04/EC08 scenarios) ----
    if state.execution_log and intent_val == "DUPLICATE_PO":
        outputs = state.execution_log.outputs or {}
        composite = outputs.get("composite_score")
        classif   = outputs.get("classification")
        action    = outputs.get("recommended_action")
        autonomy  = outputs.get("autonomy_level")
        notif_tpl = outputs.get("notification_template")
        breakdown = outputs.get("signal_breakdown")

        st.subheader("🔍 Duplicate PO detail")
        dcol1, dcol2, dcol3 = st.columns(3)
        if composite is not None:
            dcol1.metric("Composite score", f"{composite:.4f}")
        if classif:
            dcol2.metric("Classification", classif)
        if action:
            dcol3.metric("Recommended action", action)

        acol1, acol2 = st.columns(2)
        if autonomy:
            acol1.metric("Autonomy level", autonomy)
        if notif_tpl:
            acol2.metric("Notification template", notif_tpl)
        else:
            acol2.metric("Notification template", "none")

        if breakdown:
            st.markdown("**Signal breakdown:**")
            import pandas as _pd
            df = _pd.DataFrame([
                {"Signal": sig, "Weight": wt}
                for sig, wt in sorted(breakdown.items(), key=lambda x: -x[1])
            ])
            st.bar_chart(df.set_index("Signal"))

        # Batch metadata (EC08)
        meta = state.event.metadata or {}
        if meta.get("source_email_id") or meta.get("batch_po_index"):
            st.markdown("**Batch context (EC08):**")
            if meta.get("source_email_id"):
                st.write(f"Source email ID: `{meta['source_email_id']}`")
            if meta.get("batch_po_index"):
                st.write(f"Batch PO index: `{meta['batch_po_index']}`")

        st.divider()

    # ---- SKILL.md viewer ----
    skill_md = _load_skill_text(intent_val)
    if skill_md:
        with st.expander("📄 SKILL.md content"):
            st.markdown(skill_md)

    # ---- Prompt preview (shows what LLM would receive) ----
    with st.expander("🔍 Prompt preview"):
        from tests.sandbox.llm.prompts import intent_prompt, recipe_prompt, shadow_prompt
        import json as _json
        raw_meta = state.event.metadata or {}
        row_dict = {
            "order_id":    state.event.order_id,
            "event_type":  state.event.event_type,
            "retailer_id": state.event.retailer_id,
            "sku":         state.event.sku,
            "po_price":    state.event.po_price,
            "sap_price":   state.event.sap_base_price,
            "line_count":  state.event.line_count,
            "metadata":    _json.dumps(raw_meta),
        }
        st.markdown("**Intent classification prompt:**")
        st.code(intent_prompt(row_dict), language="text")
        st.markdown("**Recipe selection prompt:**")
        st.code(recipe_prompt(intent_val), language="text")
        st.markdown("**Shadow decision prompt:**")
        st.code(
            shadow_prompt(intent_val, state.event.line_count, state.batch_total_variance),
            language="text",
        )

    # ---- Full JSON trace ----
    with st.expander("📋 Full JSON trace (GraphState)"):
        st.json(state.model_dump(mode="json"))

    # ---- Gateway calls ----
    if state.resolved_data or state.effect_results:
        with st.expander("🔌 Gateway activity"):
            if state.resolved_data:
                st.markdown("**Resolved dependencies:**")
                st.json(state.resolved_data)
            if state.effect_results:
                st.markdown("**Effect results:**")
                st.json([r.model_dump() for r in state.effect_results])


# ------------------------------------------------------------------
# Data browser
# ------------------------------------------------------------------

def _render_data_browser(db: Path) -> None:
    """Show reference data tables in an expandable section."""
    with st.expander("📊 Sandbox data browser"):
        tab_cust, tab_sku, tab_promo, tab_credit = st.tabs(
            ["Customers", "SKU Master", "Promotions", "Credit Profiles"]
        )

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row

        with tab_cust:
            rows = conn.execute(
                "SELECT retailer_id, name, region, tier FROM customers "
                "WHERE active = 1 ORDER BY retailer_id"
            ).fetchall()
            if rows:
                st.dataframe([dict(r) for r in rows], use_container_width=True)

        with tab_sku:
            rows = conn.execute(
                "SELECT sku, description, category, base_price, currency, dc_id "
                "FROM sap_pricing ORDER BY sku"
            ).fetchall()
            if rows:
                st.dataframe([dict(r) for r in rows], use_container_width=True)

        with tab_promo:
            rows = conn.execute(
                "SELECT promo_id, sku, promo_type, discount_pct, start_date, end_date, region "
                "FROM promotions WHERE active = 1 ORDER BY promo_id"
            ).fetchall()
            if rows:
                st.dataframe([dict(r) for r in rows], use_container_width=True)
            else:
                st.info("No active promotions.")

        with tab_credit:
            rows = conn.execute(
                "SELECT retailer_id, credit_limit, current_exposure, risk_rating, "
                "       last_review_date "
                "FROM credit_profiles ORDER BY retailer_id"
            ).fetchall()
            if rows:
                st.dataframe([dict(r) for r in rows], use_container_width=True)

        conn.close()


# ------------------------------------------------------------------
# Auth flow validation panel
# ------------------------------------------------------------------

def _render_auth_panel(api: SandboxAPIClient) -> None:
    """Show multi-step auth flow results (matching test_auth_flow.py)."""
    with st.expander("Auth flow validation", expanded=False):
        st.markdown("**Multi-step login flow** (architecture_v3.md Section 11.1)")

        if api.auth_log:
            for entry in api.auth_log:
                icon = "PASS" if entry["status"] == 200 else "FAIL"
                st.markdown(
                    f"- **{entry['step']}** — `{entry['endpoint']}` "
                    f"-> {entry['status']} ({entry['result']}) {icon}"
                )
            if api.token:
                st.success("JWT acquired. Bearer token is active.")
            else:
                st.error("Authentication failed.")
        else:
            st.info("Run an event in API mode to see the auth flow.")

        # SSO stub check
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Test SSO init"):
                try:
                    client = api._get_client()
                    resp = client.post("/api/auth/sso/init")
                    data = resp.json()
                    if resp.status_code == 200 and "redirect_url" in data:
                        st.success(f"SSO redirect URL: `{data['redirect_url']}`")
                    else:
                        st.error(f"SSO init failed: {data}")
                except Exception as exc:
                    st.error(f"SSO init error: {exc}")

        with col2:
            if st.button("Test token refresh"):
                if not _API_AVAILABLE:
                    st.error("FastAPI not available.")
                    return
                try:
                    refresh = create_refresh_token(
                        sub="test-user", email="test@asoe.test",
                        name="Test", roles=["analyst"], org="sandbox-tenant",
                    )
                    client = api._get_client()
                    resp = client.post("/api/auth/refresh",
                                       json={"refresh_token": refresh})
                    if resp.status_code == 200:
                        data = resp.json()
                        st.success(
                            f"Refresh OK. New access token: "
                            f"`{data['access_token'][:40]}...`"
                        )
                    else:
                        st.error(f"Refresh failed: {resp.json()}")
                except Exception as exc:
                    st.error(f"Refresh error: {exc}")


# ------------------------------------------------------------------
# DB persistence panel
# ------------------------------------------------------------------

def _render_db_panel(api: SandboxAPIClient, exception_id: Optional[str]) -> None:
    """Verify exception + trace persisted after resolve (matching test_db_persistence.py)."""
    with st.expander("DB persistence verification", expanded=False):
        st.markdown("**Verify state committed to repository layer** (architecture_v3.md Section 9)")

        if not exception_id:
            st.info("Run an event in API mode to verify persistence.")
            return

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Exception record**")
            try:
                detail = api.get_exception(exception_id)
                if "id" in detail:
                    st.json(detail)
                    st.success(f"Exception `{exception_id}` persisted.")
                else:
                    st.error(f"Exception not found: {detail}")
            except Exception as exc:
                st.error(f"Fetch failed: {exc}")

        with col2:
            st.markdown("**Trace record**")
            try:
                trace = api.get_trace(exception_id)
                if "trace_id" in trace:
                    st.json(trace)
                    st.success("Trace record persisted.")
                else:
                    st.warning(f"Trace not found: {trace}")
            except Exception as exc:
                st.error(f"Trace fetch failed: {exc}")


# ------------------------------------------------------------------
# WebSocket / pub/sub event panel
# ------------------------------------------------------------------

def _render_ws_panel() -> None:
    """Show pub/sub events published during resolve (matching test_websocket_events.py)."""
    with st.expander("WebSocket / pub/sub events", expanded=False):
        st.markdown(
            "**Real-time events** published to Redis pub/sub "
            "(architecture_v3.md Section 10)"
        )

        if not _API_AVAILABLE:
            st.warning("API not available. Cannot inspect events.")
            return

        tenant = "sandbox-tenant"
        if hasattr(event_publisher, "get_recent"):
            recent = event_publisher.get_recent(tenant, limit=20)
            if recent:
                st.write(f"**{len(recent)} event(s)** in tenant `{tenant}` buffer:")
                for i, event_json in enumerate(recent):
                    parsed = json.loads(event_json)
                    event_type = parsed.get("type", "?")
                    status = parsed.get("payload", {}).get("final_status", "")
                    ts = parsed.get("timestamp", "")[:19]
                    with st.container():
                        st.markdown(
                            f"**{i+1}.** `{event_type}` — status: `{status}` — {ts}"
                        )
                        st.json(parsed)
            else:
                st.info("No events in buffer. Run an event in API mode to generate.")
        else:
            st.info("In-memory pub/sub not active.")


# ------------------------------------------------------------------
# Dashboard stats panel
# ------------------------------------------------------------------

def _render_stats_panel(api: SandboxAPIClient) -> None:
    """Dashboard metrics from GET /api/v1/exceptions/stats."""
    with st.expander("Dashboard stats", expanded=False):
        st.markdown("**Aggregate metrics** (`GET /api/v1/exceptions/stats`)")
        try:
            stats = api.get_stats()
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric("Total", stats.get("total", 0))
            c2.metric("Open", stats.get("open", 0))
            c3.metric("Auto-resolved", stats.get("auto_resolved", 0))
            c4.metric("Manual review", stats.get("manual_review", 0))
            c5.metric("Blocked", stats.get("blocked", 0))
            c6.metric("Failed", stats.get("failed", 0))
        except Exception as exc:
            st.error(f"Stats fetch failed: {exc}")


# ------------------------------------------------------------------
# Exception list panel
# ------------------------------------------------------------------

def _render_exception_list_panel(api: SandboxAPIClient) -> None:
    """Paginated exception queue from GET /api/v1/exceptions."""
    with st.expander("Exception queue (inbox)", expanded=False):
        st.markdown("**Paginated exception list** (`GET /api/v1/exceptions`)")
        try:
            data = api.list_exceptions(limit=20)
            items = data.get("data", [])
            if items:
                import pandas as _pd
                df = _pd.DataFrame(items)
                display_cols = [c for c in [
                    "id", "order_id", "intent", "lifecycle_state",
                    "shadow_verdict", "final_status", "created_at",
                ] if c in df.columns]
                st.dataframe(df[display_cols], use_container_width=True)
                st.caption(
                    f"{len(items)} shown. has_more={data.get('has_more', False)}"
                )
            else:
                st.info("No exceptions in the queue.")
        except Exception as exc:
            st.error(f"List fetch failed: {exc}")


# ------------------------------------------------------------------
# API-mode trace rendering (Lily personality support)
# ------------------------------------------------------------------

def _render_api_trace(resp: Dict[str, Any], lily: bool = False) -> None:
    """Render a resolve response from the API (matching CLI _print_api_trace)."""
    intent_val = resp.get("intent", "UNKNOWN")
    shadow_val = resp.get("shadow_verdict", "N/A")
    status_val = resp.get("final_status", "N/A")
    recipe_name = resp.get("selected_recipe") or "---"

    shadow_icon = _VERDICT_COLOUR.get(shadow_val, "")
    status_icon = _STATUS_COLOUR.get(status_val, "")

    if lily:
        st.markdown("---")
        st.markdown(f"**Lily:** I've analyzed this exception.")
        st.markdown(f"The intent is **{intent_val}**.")
        st.markdown(f"Compliance Shadow says: {shadow_icon} **{shadow_val}**.")
        if recipe_name and recipe_name != "---":
            st.markdown(f"I'm recommending **{recipe_name}**.")
        st.markdown(f"Final status: {status_icon} **{status_val}**.")
        if resp.get("explanation"):
            st.info(f"**Lily's explanation:** {resp['explanation']}")
    else:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Intent", intent_val)
        col2.metric("Shadow Verdict", f"{shadow_icon} {shadow_val}")
        col3.metric("Recipe", recipe_name)
        col4.metric("Final Status", f"{status_icon} {status_val}")

        if resp.get("explanation"):
            st.subheader("Explanation")
            st.info(resp["explanation"])

    # Execution log
    if resp.get("execution_log"):
        with st.expander("Execution log"):
            st.json(resp["execution_log"])

    # Effect results
    if resp.get("effect_results"):
        with st.expander("Effect results"):
            st.json(resp["effect_results"])

    # Full response
    with st.expander("Full API response"):
        st.json(resp)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> None:
    # Register stub gateways for DuplicatePO dependencies
    _register_sandbox_gateways()

    st.title("ASOE Execution Sandbox")
    st.caption(
        "Select a seeded EDI event (or enter a custom one) and run it "
        "through the full Skill-Shadow-Recipe pipeline. "
        "Supports Direct and API execution modes."
    )

    # Environment info banner
    backend_cls = os.getenv("LOCAL_LLM_BACKEND_CLASS", "DeterministicFallbackBackend (default)")
    explain_mode = os.getenv("ASOE_EXPLAIN_MODE", "0") == "1"
    kill_switch  = os.getenv("ASOE_KILL_SWITCH",  "0") == "1"

    langfuse_configured = bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )

    api_port = os.getenv("ASOE_API_PORT", "8000")

    with st.expander("Environment"):
        ecol1, ecol2 = st.columns(2)
        with ecol1:
            st.write(f"**LLM backend:** `{backend_cls}`")
            st.write(f"**Explain mode:** {'ON (dry-run)' if explain_mode else 'OFF'}")
            st.write(f"**Kill switch:**  {'ACTIVE' if kill_switch else 'inactive'}")
        with ecol2:
            langfuse_host = os.getenv("LANGFUSE_HOST", "cloud")
            if langfuse_configured:
                st.write(f"**LangFuse:** enabled ({langfuse_host})")
            else:
                st.write("**LangFuse:** disabled")
            if _API_AVAILABLE:
                st.write(f"**API server:** available (`uvicorn api.app:app --port {api_port}`)")
            else:
                st.write("**API server:** not installed")
            st.write(f"**DB path:** `{_db_path()}`")

    event = _render_sidebar()

    # Retrieve simulation flags
    exec_mode = st.session_state.get("exec_mode", "Direct (run_graph)")
    force_blocked = st.session_state.get("force_blocked", False)
    force_review = st.session_state.get("force_review", False)
    lily_mode = st.session_state.get("lily_mode", False)
    use_api = exec_mode == "API (REST endpoints)"

    # Initialize API client (persistent across reruns)
    if "api_client" not in st.session_state:
        st.session_state["api_client"] = SandboxAPIClient() if _API_AVAILABLE else None
    api_client: Optional[SandboxAPIClient] = st.session_state["api_client"]

    # Data browser (always visible)
    db = _db_path()
    if db.exists():
        _render_data_browser(db)

    if event is None:
        st.info("Select an event in the sidebar and click **Run event** to begin.")

        # Show API panels even without a run (for auth testing)
        if use_api and api_client and _API_AVAILABLE:
            _render_auth_panel(api_client)
            _render_stats_panel(api_client)
            _render_exception_list_panel(api_client)
            _render_ws_panel()
        return

    # ── Apply simulation flags ──
    if force_blocked:
        event = OrderEvent(**{**event.model_dump(), "line_count": 50})
    if force_review:
        os.environ["ASOE_EXPLAIN_MODE"] = "1"

    exception_id: Optional[str] = None

    if use_api and api_client:
        # ── API mode ──
        with st.spinner("Authenticating and running via REST API..."):
            try:
                if not api_client.token:
                    api_client.authenticate()

                event_payload = {
                    "order_id": event.order_id,
                    "event_type": event.event_type,
                    "sku": event.sku,
                    "po_price": event.po_price,
                    "sap_base_price": event.sap_base_price,
                    "retailer_id": event.retailer_id,
                    "requester_role": event.requester_role,
                    "credit_limit": event.credit_limit,
                    "current_exposure": event.current_exposure,
                    "line_count": event.line_count,
                    "metadata": event.metadata or {},
                }
                resp_data = api_client.resolve(event_payload,
                                                explain=force_review)
                exception_id = resp_data.get("exception_id")

            except Exception as exc:
                st.error(f"API execution error: {exc}")
                if force_review:
                    os.environ.pop("ASOE_EXPLAIN_MODE", None)
                return

        if force_review:
            os.environ.pop("ASOE_EXPLAIN_MODE", None)

        _render_api_trace(resp_data, lily=lily_mode)

        # Show API-specific panels
        st.divider()
        _render_auth_panel(api_client)
        _render_db_panel(api_client, exception_id)
        _render_stats_panel(api_client)
        _render_exception_list_panel(api_client)
        _render_ws_panel()

    else:
        # ── Direct mode ──
        initial_state = GraphState(event=event)

        with st.spinner("Running pipeline..."):
            try:
                final_state = run_graph(initial_state)
            except Exception as exc:
                st.error(f"Pipeline error: {exc}")
                st.exception(exc)
                if force_review:
                    os.environ.pop("ASOE_EXPLAIN_MODE", None)
                return

        if force_review:
            os.environ.pop("ASOE_EXPLAIN_MODE", None)

        if lily_mode:
            intent_val = final_state.intent.value if final_state.intent else "UNKNOWN"
            shadow_val = final_state.shadow.status.value if final_state.shadow else "N/A"
            status_val = final_state.final_status.value if final_state.final_status else "N/A"
            recipe_name = final_state.selected_recipe or "---"
            shadow_icon = _VERDICT_COLOUR.get(shadow_val, "")
            status_icon = _STATUS_COLOUR.get(status_val, "")

            st.markdown("---")
            st.markdown(f"**Lily:** I've analyzed this exception.")
            st.markdown(f"The intent is **{intent_val}**.")
            st.markdown(f"Compliance Shadow says: {shadow_icon} **{shadow_val}**.")
            if recipe_name and recipe_name != "---":
                st.markdown(f"I'm recommending **{recipe_name}**.")
            st.markdown(f"Final status: {status_icon} **{status_val}**.")
            if final_state.explanation:
                st.info(f"**Lily's explanation:** {final_state.explanation}")
        else:
            _render_trace(final_state)


if __name__ == "__main__":
    main()
