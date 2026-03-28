"""ASOE Sandbox — Streamlit execution trace visualiser.

Launch
------
    cd /path/to/asoe
    PYTHONPATH=. streamlit run tests/sandbox/ui/app.py

Optional env vars
-----------------
    SANDBOX_DB_PATH           Path to sandbox.db (default: tests/sandbox/sandbox.db)
    LOCAL_LLM_BACKEND_CLASS   e.g. tests.sandbox.llm.local_backend.LocalHFBackend
    LOCAL_LLM_MODEL           HuggingFace model id (default: Qwen/Qwen2.5-0.5B-Instruct)
    ASOE_EXPLAIN_MODE         1 = dry-run mode (no recipe side effects)
    LANGFUSE_PUBLIC_KEY       LangFuse public key (enables trace forwarding)
    LANGFUSE_SECRET_KEY       LangFuse secret key (required alongside public key)
    LANGFUSE_HOST             LangFuse host URL (omit for LangFuse Cloud)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# ------------------------------------------------------------------
# Ensure repo root is on sys.path regardless of how streamlit is run.
# ------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

# ---- ASOE imports (after path fix) --------------------------------
from contracts.models import GraphState, Intent, OrderEvent
from orchestration.graph import run_graph
from tests.sandbox.seed import (
    load_events,
    load_customers,
    load_promotions,
    lookup_customer,
    lookup_credit_profile,
    DB_DEFAULT,
)

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
    return "UNKNOWN"


# ------------------------------------------------------------------
# Sidebar — event picker or custom form
# ------------------------------------------------------------------

def _render_sidebar() -> Optional[OrderEvent]:
    st.sidebar.header("⚙️  ASOE Sandbox")

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
        ])
        sku         = st.text_input("sku",            value="SKU-001")
        po_price    = st.number_input("po_price",     value=90.0, step=0.01)
        sap_price   = st.number_input("sap_price",    value=100.0, step=0.01)
        retailer_id = st.text_input("retailer_id",    value="R-01")
        line_count  = st.number_input("line_count",   value=1, min_value=1, step=1)
        submitted   = st.form_submit_button("▶  Run event", type="primary")

    if submitted:
        return OrderEvent(
            order_id=order_id,
            event_type=event_type,
            sku=sku,
            po_price=float(po_price),
            sap_base_price=float(sap_price),
            retailer_id=retailer_id,
            line_count=int(line_count),
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
# Main
# ------------------------------------------------------------------

def main() -> None:
    st.title("⚙️  ASOE Execution Sandbox")
    st.caption(
        "Select a seeded EDI event (or enter a custom one) and run it "
        "through the full Skill–Shadow–Recipe pipeline."
    )

    # Environment info banner
    backend_cls = os.getenv("LOCAL_LLM_BACKEND_CLASS", "DeterministicFallbackBackend (default)")
    explain_mode = os.getenv("ASOE_EXPLAIN_MODE", "0") == "1"
    kill_switch  = os.getenv("ASOE_KILL_SWITCH",  "0") == "1"

    langfuse_configured = bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )

    with st.expander("ℹ️  Environment"):
        st.write(f"**LLM backend:** `{backend_cls}`")
        st.write(f"**Explain mode:** {'ON (dry-run)' if explain_mode else 'OFF'}")
        st.write(f"**Kill switch:**  {'🔴 ACTIVE' if kill_switch else '✅ inactive'}")
        langfuse_host = os.getenv("LANGFUSE_HOST", "cloud")
        if langfuse_configured:
            st.write(f"**LangFuse:** ✅ enabled ({langfuse_host})")
        else:
            st.write("**LangFuse:** disabled (set `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY`)")
        st.write(f"**DB path:** `{_db_path()}`")

    event = _render_sidebar()

    # Data browser (always visible)
    db = _db_path()
    if db.exists():
        _render_data_browser(db)

    if event is None:
        st.info("Select an event in the sidebar and click **▶ Run event** to begin.")
        return

    initial_state = GraphState(event=event)

    with st.spinner("Running pipeline…"):
        try:
            final_state = run_graph(initial_state)
        except Exception as exc:
            st.error(f"Pipeline error: {exc}")
            st.exception(exc)
            return

    _render_trace(final_state)


if __name__ == "__main__":
    main()
