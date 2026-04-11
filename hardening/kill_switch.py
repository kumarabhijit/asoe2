from __future__ import annotations

# Phase 6 — Kill switch
#
# A global emergency stop that halts ALL automated recipe execution before
# any graph node is invoked.
#
# Activation:
#   Set the environment variable ASOE_KILL_SWITCH=1 (or "true" / "yes").
#   The check is evaluated at run_graph() call time, not at import time,
#   so the switch takes effect immediately without a process restart.
#
# Behaviour when active:
#   run_graph() returns immediately with:
#     final_status  = FAIL_TO_HUMAN
#     explanation   = kill-switch reason string
#   No nodes are executed.  No LLM calls are made.  No recipes run.
#
# Auditor notes:
#   - The env var is the single source of truth; no database or file needed.
#   - The switch is intentionally coarse-grained: it stops everything.
#   - Partial kill (per-intent, per-recipe) is out of scope for Phase 6.
#   - The kill-switch state is recorded in every TraceRecord emitted by
#     the observability scaffold (final_status=FAIL_TO_HUMAN,
#     explanation references ASOE_KILL_SWITCH).

from contracts.models import GraphState, TerminalStatus
from hardening import is_env_truthy

_KILL_SWITCH_ENV = "ASOE_KILL_SWITCH"

KILL_SWITCH_EXPLANATION = (
    "Automated execution halted: ASOE_KILL_SWITCH is active. "
    "All recipe execution is disabled by operator configuration. "
    "Route to human review."
)


def is_kill_switch_active() -> bool:
    """Return True if the kill switch env var is set to a truthy value."""
    return is_env_truthy(_KILL_SWITCH_ENV)


def apply_kill_switch(state: GraphState) -> GraphState:
    """Return a copy of *state* with FAIL_TO_HUMAN and kill-switch explanation.

    Called by run_graph() when is_kill_switch_active() is True.  The original
    state object is not mutated; a new GraphState is returned.
    """
    return state.model_copy(update={
        "final_status": TerminalStatus.FAIL_TO_HUMAN,
        "explanation": KILL_SWITCH_EXPLANATION,
    })
