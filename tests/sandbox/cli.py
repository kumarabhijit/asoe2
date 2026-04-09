"""ASOE Sandbox — headless CLI runner.

Runs sandbox scenarios through the full Skill-Shadow-Recipe pipeline and
prints results and detailed execution traces to the terminal.

Usage
-----
    # Run all seeded events
    PYTHONPATH=. python tests/sandbox/cli.py

    # Run a single event by event_id
    PYTHONPATH=. python tests/sandbox/cli.py --event EVT-CC-001

    # Run events matching an intent prefix
    PYTHONPATH=. python tests/sandbox/cli.py --intent CONTRACTUAL_CORRECTION

    # Show full JSON trace for each event
    PYTHONPATH=. python tests/sandbox/cli.py --json

    # Show prompt previews
    PYTHONPATH=. python tests/sandbox/cli.py --prompts

    # Custom DB path
    PYTHONPATH=. python tests/sandbox/cli.py --db /tmp/test.db

    # Combine flags
    PYTHONPATH=. python tests/sandbox/cli.py --event EVT-CC-001 --json --prompts

Optional env vars
-----------------
    SANDBOX_DB_PATH           Path to sandbox.db (default: tests/sandbox/sandbox.db)
    LOCAL_LLM_BACKEND_CLASS   e.g. tests.sandbox.llm.local_backend.LocalHFBackend
    ASOE_EXPLAIN_MODE         1 = dry-run mode (no recipe side effects)
    ASOE_KILL_SWITCH          1 = kill switch active
    LANGFUSE_PUBLIC_KEY       LangFuse public key (enables trace forwarding)
    LANGFUSE_SECRET_KEY       LangFuse secret key (required alongside public key)
    LANGFUSE_HOST             LangFuse host URL (omit for LangFuse Cloud)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from contracts.models import GatewayResponse, GraphState, Intent, OrderEvent
from gateways.registry import register_gateway, clear_registry
from gateways.stub import StubGateway
from orchestration.graph import run_graph
from tests.sandbox.seed import load_events, DB_DEFAULT
from tests.sandbox.llm.prompts import intent_prompt, recipe_prompt, shadow_prompt


def _register_sandbox_gateways() -> None:
    """Register stub gateways for DuplicatePO resolution context.

    The DuplicatePORecipe declares gateway dependencies (oms/get_fulfillment_status,
    oms/get_matched_po_details) and effects (buyer_notification/send).  These stubs
    provide canned responses so the sandbox CLI runs without real OMS/notification
    connectivity.
    """
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

# ── ANSI colours ──────────────────────────────────────────────────────────

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_VERDICT_STYLE = {
    "GREEN": f"{_GREEN}GREEN{_RESET}",
    "YELLOW": f"{_YELLOW}YELLOW{_RESET}",
    "RED": f"{_RED}RED{_RESET}",
}

_STATUS_STYLE = {
    "COMPLETE": f"{_GREEN}COMPLETE{_RESET}",
    "FAIL_TO_HUMAN": f"{_RED}FAIL_TO_HUMAN{_RESET}",
    "MANUAL_REVIEW_REQUIRED": f"{_YELLOW}MANUAL_REVIEW_REQUIRED{_RESET}",
    "BLOCKED": f"{_RED}BLOCKED{_RESET}",
    "REJECTED": f"{_RED}REJECTED{_RESET}",
}


# ── Helpers ───────────────────────────────────────────────────────────────

def _db_path(override: Optional[str] = None) -> Path:
    if override:
        return Path(override)
    custom = os.getenv("SANDBOX_DB_PATH", "")
    return Path(custom) if custom else DB_DEFAULT


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


def _edi_row_to_order_event(row: Dict[str, Any]) -> OrderEvent:
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


def _hr(char: str = "─", width: int = 72) -> str:
    return char * width


# ── Trace rendering ──────────────────────────────────────────────────────

def _print_trace(state: GraphState, *, show_json: bool = False,
                 show_prompts: bool = False) -> None:
    intent_val = state.intent.value if state.intent else "UNKNOWN"
    shadow_val = state.shadow.status.value if state.shadow else "N/A"
    status_val = state.final_status.value if state.final_status else "N/A"
    recipe_name = state.selected_recipe or "—"

    shadow_styled = _VERDICT_STYLE.get(shadow_val, shadow_val)
    status_styled = _STATUS_STYLE.get(status_val, status_val)

    # Header summary
    print(f"\n  {_BOLD}Intent:{_RESET}          {intent_val}")
    print(f"  {_BOLD}Shadow Verdict:{_RESET}  {shadow_styled}")
    print(f"  {_BOLD}Recipe:{_RESET}          {recipe_name}")
    print(f"  {_BOLD}Final Status:{_RESET}    {status_styled}")

    # Execution trace steps
    print(f"\n  {_BOLD}Execution Trace{_RESET}")
    print(f"  {_hr('─', 60)}")

    steps = [
        ("1. Ingest",
         f"order_id={state.event.order_id}  event_type={state.event.event_type}"),
        ("2. Classify",
         f"intent={intent_val}  confidence={state.confidence:.2f}"),
        ("3. Load Skill",
         f"skill={state.skill.name if state.skill else '—'}"),
        ("4. Circuit Breaker",
         f"batch_variance={state.batch_total_variance:.2f}"),
        ("5. Compliance Shadow",
         f"verdict={shadow_val}  "
         f"policy_hits={state.shadow.policy_hits if state.shadow else []}"),
        ("6. Select Recipe",
         f"recipe={recipe_name}"),
        ("7. Resolve Dependencies",
         f"keys={list(state.resolved_data.keys()) or 'none'}"),
        ("8. Execute Recipe",
         f"status={status_val}"),
        ("9. Apply Effects",
         f"effects={len(state.effect_results)}"),
    ]

    for label, detail in steps:
        print(f"  {_DIM}│{_RESET} {label:<25s} {detail}")

    # Shadow detail
    if state.shadow:
        print(f"\n  {_BOLD}Compliance Shadow Detail{_RESET}")
        print(f"  Reasons:     {state.shadow.reasons}")
        print(f"  Policy hits: {state.shadow.policy_hits}")

    # DPO-specific detail (EC04/EC08 scenarios)
    if (state.execution_log
            and state.intent
            and state.intent.value == "DUPLICATE_PO"):
        outputs = state.execution_log.outputs or {}
        composite   = outputs.get("composite_score")
        classif     = outputs.get("classification")
        action      = outputs.get("recommended_action")
        autonomy    = outputs.get("autonomy_level")
        notif_tpl   = outputs.get("notification_template")
        breakdown   = outputs.get("signal_breakdown")

        print(f"\n  {_BOLD}Duplicate PO Detail{_RESET}")
        if composite is not None:
            print(f"  Composite score:     {composite:.4f}")
        if classif:
            print(f"  Classification:      {classif}")
        if action:
            print(f"  Recommended action:  {action}")
        if autonomy:
            print(f"  Autonomy level:      {autonomy}")
        if notif_tpl:
            print(f"  Notification:        {notif_tpl}")
        else:
            print(f"  Notification:        {_DIM}none{_RESET}")
        if breakdown:
            print(f"  Signal breakdown:")
            for signal, weight in sorted(breakdown.items(), key=lambda x: -x[1]):
                bar = "█" * int(weight * 40) + "░" * (40 - int(weight * 40))
                print(f"    {signal:<16s} {weight:.4f}  {_DIM}{bar}{_RESET}")

        # Batch metadata (EC08)
        meta = state.event.metadata or {}
        if meta.get("source_email_id") or meta.get("batch_po_index"):
            print(f"\n  {_BOLD}Batch Context (EC08){_RESET}")
            if meta.get("source_email_id"):
                print(f"  Source email ID:     {meta['source_email_id']}")
            if meta.get("batch_po_index"):
                print(f"  Batch PO index:      {meta['batch_po_index']}")

    # Explanation
    if state.explanation:
        print(f"\n  {_BOLD}Explanation{_RESET}")
        print(f"  {state.explanation}")

    # Gateway activity
    if state.resolved_data or state.effect_results:
        print(f"\n  {_BOLD}Gateway Activity{_RESET}")
        if state.resolved_data:
            print(f"  Resolved dependencies: {json.dumps(state.resolved_data, indent=4, default=str)}")
        if state.effect_results:
            print(f"  Effect results: {json.dumps([r.model_dump() for r in state.effect_results], indent=4, default=str)}")

    # Prompt preview
    if show_prompts:
        print(f"\n  {_BOLD}Prompt Previews{_RESET}")
        raw_meta = state.event.metadata or {}
        row_dict = {
            "order_id": state.event.order_id,
            "event_type": state.event.event_type,
            "retailer_id": state.event.retailer_id,
            "sku": state.event.sku,
            "po_price": state.event.po_price,
            "sap_price": state.event.sap_base_price,
            "line_count": state.event.line_count,
            "metadata": json.dumps(raw_meta),
        }
        print(f"\n  {_CYAN}--- Intent classification prompt ---{_RESET}")
        print(intent_prompt(row_dict))
        print(f"\n  {_CYAN}--- Recipe selection prompt ---{_RESET}")
        print(recipe_prompt(intent_val))
        print(f"\n  {_CYAN}--- Shadow decision prompt ---{_RESET}")
        print(shadow_prompt(intent_val, state.event.line_count, state.batch_total_variance))

    # Full JSON trace
    if show_json:
        print(f"\n  {_BOLD}Full JSON Trace (GraphState){_RESET}")
        print(json.dumps(state.model_dump(mode="json"), indent=2, default=str))


# ── Summary table ────────────────────────────────────────────────────────

def _print_summary(results: List[Dict[str, Any]]) -> None:
    """Print a compact summary table of all executed events."""
    print(f"\n{'=' * 80}")
    print(f"  {_BOLD}SUMMARY{_RESET}")
    print(f"{'=' * 80}")

    # Column headers
    hdr = f"  {'EVENT ID':<16s} {'INTENT':<26s} {'SHADOW':<10s} {'STATUS':<28s}"
    print(hdr)
    print(f"  {_hr('─', 76)}")

    pass_count = 0
    fail_count = 0
    error_count = 0

    for r in results:
        if r.get("error"):
            status_str = f"{_RED}ERROR{_RESET}"
            error_count += 1
        else:
            shadow_val = r["shadow"]
            status_val = r["status"]
            shadow_str = _VERDICT_STYLE.get(shadow_val, shadow_val)
            status_str = _STATUS_STYLE.get(status_val, status_val)
            if status_val == "COMPLETE":
                pass_count += 1
            else:
                fail_count += 1

        event_id = r.get("event_id", "?")
        intent = r.get("intent", "?")
        shadow_display = shadow_str if not r.get("error") else "—"
        status_display = status_str

        print(f"  {event_id:<16s} {intent:<26s} {shadow_display:<22s} {status_display}")

    total = len(results)
    print(f"  {_hr('─', 76)}")
    print(f"  Total: {total}  |  "
          f"{_GREEN}COMPLETE: {pass_count}{_RESET}  |  "
          f"{_YELLOW}Non-complete: {fail_count}{_RESET}  |  "
          f"{_RED}Errors: {error_count}{_RESET}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="ASOE Sandbox — headless CLI test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  PYTHONPATH=. python tests/sandbox/cli.py\n"
            "  PYTHONPATH=. python tests/sandbox/cli.py --event EVT-CC-001\n"
            "  PYTHONPATH=. python tests/sandbox/cli.py --intent CREDIT_BLOCK --json\n"
            "  PYTHONPATH=. python tests/sandbox/cli.py --event EVT-DPO-001 --prompts\n"
        ),
    )
    parser.add_argument("--db", default=None,
                        help="Path to sandbox.db (default: tests/sandbox/sandbox.db)")
    parser.add_argument("--event", default=None,
                        help="Run a single event by event_id (e.g. EVT-CC-001)")
    parser.add_argument("--intent", default=None,
                        help="Filter events by intent prefix "
                             "(CONTRACTUAL_CORRECTION, CREDIT_BLOCK, "
                             "MASS_PRICING_ERROR, DUPLICATE_PO)")
    parser.add_argument("--json", action="store_true", dest="show_json",
                        help="Print full JSON trace for each event")
    parser.add_argument("--prompts", action="store_true",
                        help="Print LLM prompt previews for each event")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-event trace; show only summary table")
    parser.add_argument("--langfuse-flush", action="store_true",
                        dest="langfuse_flush",
                        help="Flush LangFuse traces before exit (requires "
                             "LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY)")

    args = parser.parse_args()

    # Register stub gateways for DuplicatePO dependencies
    _register_sandbox_gateways()

    db = _db_path(args.db)
    if not db.exists():
        print(f"{_RED}ERROR:{_RESET} sandbox.db not found at {db}")
        print("Run:  python tests/sandbox/seed.py")
        return 1

    # Load and filter events
    all_events = load_events(db)
    if not all_events:
        print(f"{_RED}ERROR:{_RESET} No events found in {db}")
        return 1

    events = all_events
    if args.event:
        events = [e for e in all_events if e.get("event_id") == args.event]
        if not events:
            print(f"{_RED}ERROR:{_RESET} Event '{args.event}' not found.")
            print(f"Available: {', '.join(e['event_id'] for e in all_events)}")
            return 1
    elif args.intent:
        intent_prefix_map = {
            "CONTRACTUAL_CORRECTION": "EVT-CC-",
            "CREDIT_BLOCK": "EVT-CB-",
            "MASS_PRICING_ERROR": "EVT-MPE-",
            "DUPLICATE_PO": "EVT-DPO-",
        }
        prefix = intent_prefix_map.get(args.intent.upper())
        if not prefix:
            print(f"{_RED}ERROR:{_RESET} Unknown intent '{args.intent}'.")
            print(f"Allowed: {', '.join(intent_prefix_map.keys())}")
            return 1
        events = [e for e in all_events
                  if e.get("event_id", "").startswith(prefix)]
        if not events:
            print(f"{_YELLOW}WARN:{_RESET} No events match intent {args.intent}")
            return 1

    # Environment banner
    backend_cls = os.getenv("LOCAL_LLM_BACKEND_CLASS",
                            "DeterministicFallbackBackend (default)")
    explain_mode = os.getenv("ASOE_EXPLAIN_MODE", "0") == "1"
    kill_switch = os.getenv("ASOE_KILL_SWITCH", "0") == "1"
    langfuse_configured = bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )

    # API server availability
    api_available = False
    try:
        from api.app import app as _api_app  # noqa: F401
        api_available = True
    except ImportError:
        pass
    api_port = os.getenv("ASOE_API_PORT", "8000")

    print(f"\n{'=' * 80}")
    print(f"  {_BOLD}ASOE Sandbox — CLI Runner{_RESET}")
    print(f"{'=' * 80}")
    print(f"  LLM backend:   {backend_cls}")
    print(f"  Explain mode:  {'ON (dry-run)' if explain_mode else 'OFF'}")
    print(f"  Kill switch:   {f'{_RED}ACTIVE{_RESET}' if kill_switch else 'inactive'}")
    langfuse_host = os.getenv("LANGFUSE_HOST", "cloud")
    if langfuse_configured:
        print(f"  LangFuse:      {_GREEN}enabled{_RESET} ({langfuse_host})")
    else:
        print(f"  LangFuse:      {_DIM}disabled (set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY){_RESET}")
    if api_available:
        print(f"  API server:    {_GREEN}available{_RESET} (uvicorn api.app:app --port {api_port})")
    else:
        print(f"  API server:    {_DIM}not installed (pip install fastapi uvicorn){_RESET}")
    print(f"  DB path:       {db}")
    print(f"  Events:        {len(events)}")
    print(f"{'=' * 80}")

    # Execute each event
    results: List[Dict[str, Any]] = []

    for i, row in enumerate(events, 1):
        event_id = row.get("event_id", "?")
        intent_lbl = _intent_label(row)

        if not args.quiet:
            print(f"\n{'─' * 80}")
            print(f"  [{i}/{len(events)}] {_BOLD}{event_id}{_RESET}"
                  f"  ({row['order_id']} · {intent_lbl})")
            print(f"{'─' * 80}")

        try:
            event = _edi_row_to_order_event(row)
            initial_state = GraphState(event=event)
            final_state = run_graph(initial_state)

            intent_val = final_state.intent.value if final_state.intent else "UNKNOWN"
            shadow_val = (final_state.shadow.status.value
                          if final_state.shadow else "N/A")
            status_val = (final_state.final_status.value
                          if final_state.final_status else "N/A")

            results.append({
                "event_id": event_id,
                "intent": intent_val,
                "shadow": shadow_val,
                "status": status_val,
            })

            if not args.quiet:
                _print_trace(final_state,
                             show_json=args.show_json,
                             show_prompts=args.prompts)

        except Exception as exc:
            results.append({
                "event_id": event_id,
                "intent": intent_lbl,
                "shadow": "N/A",
                "status": "ERROR",
                "error": str(exc),
            })
            if not args.quiet:
                print(f"\n  {_RED}ERROR:{_RESET} {exc}")
                traceback.print_exc()

    # Summary table
    _print_summary(results)

    # Flush LangFuse traces if requested
    if args.langfuse_flush:
        try:
            from observability.langfuse_sink import flush
            flush()
            if langfuse_configured:
                print(f"  {_GREEN}LangFuse traces flushed.{_RESET}\n")
        except Exception as exc:
            print(f"  {_YELLOW}LangFuse flush failed: {exc}{_RESET}\n")

    # Exit code: 0 if no errors, 1 otherwise
    has_errors = any(r.get("error") for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
