from __future__ import annotations

# Phase 6 — Read-only explain mode
#
# When explain mode is active the graph executes the full reasoning pipeline
# (ingest → classify → load_skill → validate_circuit_breaker → shadow_audit
#  → select_recipe → validate_types) but stops before execute_recipe.
#
# The explain_only node replaces execute_recipe in the graph.  It records
# what WOULD have been done — recipe name, invocation parameters, shadow
# verdict, intent — and sets:
#   final_status  = MANUAL_REVIEW_REQUIRED
#   explanation   = human-readable dry-run summary
#
# No SAP/ERP writes, no MCP calls, no recipe side-effects.
#
# Activation:
#   Set the environment variable ASOE_EXPLAIN_MODE=1 (or "true" / "yes").
#   Evaluated at run_graph() call time; no restart needed.
#
# Auditor notes:
#   - Explain mode is safe to run in production for impact assessment.
#   - The Compliance Shadow DOES run in explain mode: you get the real
#     shadow verdict (GREEN / YELLOW / RED) with no execution consequence.
#   - The circuit breaker DOES run: runaway-batch protection is preserved.
#   - TraceRecord is emitted as normal; final_status = MANUAL_REVIEW_REQUIRED.
#   - Intended for: auditor dry-runs, operator review, CI smoke-tests.

import os
from typing import Optional

_EXPLAIN_MODE_ENV = "ASOE_EXPLAIN_MODE"
_TRUTHY = {"1", "true", "yes"}


def is_explain_mode_active() -> bool:
    """Return True if explain mode env var is set to a truthy value."""
    return os.getenv(_EXPLAIN_MODE_ENV, "0").strip().lower() in _TRUTHY


def build_explain_summary(
    intent: Optional[str],
    shadow_verdict: Optional[str],
    shadow_policy_hits: list,
    selected_recipe: Optional[str],
    invocation_params: Optional[dict],
) -> str:
    """Compose a human-readable dry-run explanation string.

    This is the explanation attached to the MANUAL_REVIEW_REQUIRED terminal
    state when explain mode halts the graph before recipe execution.
    """
    lines = [
        "EXPLAIN MODE — dry-run only, no recipe was executed.",
        f"  Intent classified : {intent or 'UNKNOWN'}",
        f"  Shadow verdict    : {shadow_verdict or 'not reached'}",
    ]
    if shadow_policy_hits:
        lines.append(f"  Shadow policy hits: {', '.join(shadow_policy_hits)}")
    if selected_recipe:
        lines.append(f"  Would execute     : {selected_recipe}")
    if invocation_params:
        lines.append(f"  With params       : {invocation_params}")
    else:
        lines.append("  With params       : (not yet validated)")
    lines.append("Activate without ASOE_EXPLAIN_MODE to execute.")
    return "\n".join(lines)
