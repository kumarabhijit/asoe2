"""DoR gate #9 (Phase 8) — business/disposition events stay hash-chained.

The hash-chain mechanism (`exception_store.log_audit_event` →
`verify_audit_chain`) and its tamper-evidence are covered by
`tests/test_audit_chain.py`; the ADR-042 disposition write paths were wired into
that same per-tenant chain incrementally (Phases 3–4). The original gate concern
was "today only `policy_audit_log` is chained" — i.e. business/disposition
events might NOT be.

This lock keeps that closed: each ADR-042 disposition persist function MUST emit
a chained audit event, so a future refactor can't silently drop business-event
chaining and regress the SOX trail. It's a source-level (AST) guard — cheap,
and it fails loudly the moment a persist path stops calling `log_audit_event`.
"""

from __future__ import annotations

import ast
from pathlib import Path

_EXC = Path(__file__).resolve().parent.parent / "api" / "routes" / "exceptions.py"

# ADR-042 disposition persist functions that MUST chain a business audit event.
_MUST_CHAIN = {
    "_persist_erp_submit",   # SUBMIT_TO_ERP disposition
    "_persist_reply_draft",  # DRAFT_REPLY disposition
    "_persist_reply_sent",   # SEND_REPLY disposition
}


def _functions_calling_log_audit_event() -> set[str]:
    tree = ast.parse(_EXC.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "log_audit_event"
            ):
                out.add(node.name)
                break
    return out


def test_adr042_disposition_handlers_chain_audit_events() -> None:
    chaining = _functions_calling_log_audit_event()
    missing = _MUST_CHAIN - chaining
    assert not missing, (
        f"business/disposition handlers no longer chain audit events: {sorted(missing)} "
        "— each must call exception_store.log_audit_event so the SOX hash chain "
        "covers business events, not just policy_audit_log (DoR #9)."
    )
