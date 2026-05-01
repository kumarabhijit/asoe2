from __future__ import annotations

# Phase 4 — LangGraph state machine
#
# build_graph()         — returns the compiled StateGraph (normal execution).
# build_explain_graph() — same pipeline, but execute_recipe is replaced with
#                         explain_only (Phase 6 explain mode).
# run_graph()           — convenience wrapper: checks Phase 6 kill switch and
#                         explain mode, invokes the appropriate graph, and
#                         returns a typed GraphState.
#
# State transitions (all deterministic, no hidden loops):
#   ingest → classify → load_skill → validate_circuit_breaker
#     ├─ [breach]   → build_analysis → END (FAIL_TO_HUMAN)
#     └─ [ok]       → select_recipe
#                       ├─ [no recipe] → build_analysis → END (FAIL_TO_HUMAN)
#                       └─ [ok]        → resolve_dependencies
#                                          ├─ [required-gw fail] → build_analysis → END (FAIL_TO_HUMAN)
#                                          └─ [ok]               → validate_types
#                                                                     ├─ [invocation fail] → build_analysis → END
#                                                                     └─ [ok]              → shadow_audit
#                                                                                              ├─ [RED]     → build_analysis → END (BLOCKED, with audit evidence)
#                                                                                              ├─ [YELLOW]  → build_analysis → END (MANUAL_REVIEW_REQUIRED, with audit evidence)
#                                                                                              └─ [GREEN]   → execute_recipe → apply_effects → build_analysis → END
#                                                                                                              (explain mode: explain_only → build_analysis → END)
#
# The reorder (2026-04-22 follow-up) places gateway READS
# (resolve_dependencies) BEFORE shadow_audit so audit-bearing
# evidence is captured for every record regardless of compliance
# verdict. Shadow still gates *recipe execution and effects* per
# CLAUDE.md Guardrail #4 — that is its load-bearing job; gateway
# reads are evidence acquisition, not execution.
#
# Phase 6 hardening:
#   kill switch  — run_graph() returns FAIL_TO_HUMAN before any node executes
#                  when ASOE_KILL_SWITCH=1
#   explain mode — build_explain_graph() replaces execute_recipe with
#                  explain_only when ASOE_EXPLAIN_MODE=1; returns
#                  MANUAL_REVIEW_REQUIRED with a dry-run summary

from functools import cache
from typing import Mapping

from langgraph.graph import END, StateGraph
from contracts.models import GraphState
from orchestration import nodes
from hardening.kill_switch import is_kill_switch_active, apply_kill_switch
from hardening.explain_mode import is_explain_mode_active


def route_after_gate(state: GraphState) -> str:
    """Route to terminal END if any node set final_status, else continue."""
    return "terminal" if state.final_status is not None else "continue"


# ---------------------------------------------------------------------------
# ADR-027 Phase A.0 — verdict-vocabulary registration
# ---------------------------------------------------------------------------
#
# `route_after_gate` collapses every conditional into {terminal, continue}
# based on `state.final_status`. The HUMAN verdict that drove the route
# (GREEN | YELLOW | RED, breach | ok, no recipe | ok, …) lives on state
# side-effects (`state.shadow.status`, ad-hoc `final_status` reasons) and
# never reaches the compiled-graph topology. Compiled-graph introspection
# alone therefore exposes only `terminal/continue` keys — insufficient for
# the audit-first DAG view ADR-027 ships.
#
# This registry is the load-bearing prerequisite for ADR-027 Phase A: it
# declares per-gate verdict labels alongside each `add_conditional_edges`
# call so Phase A's introspection helper can join them with the compiled
# graph and produce a verdict-labelled `PipelineTopology`. The per-record
# execution trace (Phase B) supplies which verdict actually fired on each
# record's `executed_nodes` entry.
#
# Layout: gate_node -> {verdict_route_key: downstream_node}
#   - `terminal_*` keys correspond to the `terminal` branch of
#     `route_after_gate` (the gate halted execution).
#   - `continue_*` keys correspond to the `continue` branch (the gate
#     allowed execution to proceed).
#   - The verdict suffix (e.g. `_red`, `_breach`, `_no_recipe`) is the
#     human label that will appear on the DAG edge.
#
# The shadow_audit gate has TWO terminal verdicts (RED and YELLOW) sharing
# the same `terminal` route — both target `build_analysis` but the verdict
# label on the edge differs per record. The introspection helper resolves
# which terminal verdict fired by reading the `ExecutedNode` for
# shadow_audit (its `decision`/`exit_verdict`), not by reading the edge.
#
# Live-graph form. The explain-mode graph swaps `execute_recipe` →
# `explain_only` on the GREEN continuation; that swap is owned by
# `build_explain_graph` and applied by Phase A's introspection helper at
# topology-fetch time. v4.1 may register an explain-overrides map if
# explain-mode topology becomes a separately-rendered surface.
_VERDICT_LABELS: Mapping[str, Mapping[str, str]] = {
    "validate_circuit_breaker": {
        "terminal_breach": "build_analysis",
        "continue_ok": "select_recipe",
    },
    "select_recipe": {
        "terminal_no_recipe": "build_analysis",
        "continue_ok": "resolve_dependencies",
    },
    "resolve_dependencies": {
        "terminal_required_gw_fail": "build_analysis",
        "continue_ok": "validate_types",
    },
    "validate_types": {
        "terminal_invocation_fail": "build_analysis",
        "continue_ok": "shadow_audit",
    },
    "shadow_audit": {
        "terminal_red": "build_analysis",
        "terminal_yellow": "build_analysis",
        "continue_green": "execute_recipe",
    },
}

# Implicit gates — nodes that set `final_status` themselves rather than
# routing via `add_conditional_edges`. The compiled graph has no edge to
# attach a verdict to; Phase D's DAG renderer draws an implicit highlighted
# edge (classify → build_analysis) when an `ExecutedNode` for classify
# carries `exit_verdict="cross_check_disagreement"` (architecture_v4 §4
# v4 amendment, ADR-027 Open Question §7).
#
# Registered here so the verdict vocabulary is the single source of truth
# across both explicit (add_conditional_edges) and implicit (final_status-
# set-in-node) gates — ADR-027 Phase A.0 review-board criterion.
_IMPLICIT_VERDICT_LABELS: Mapping[str, Mapping[str, str]] = {
    "classify": {
        "implicit_terminal_cross_check_disagreement": "build_analysis",
        "implicit_continue_ok": "load_skill",
    },
}


def get_verdict_labels() -> Mapping[str, Mapping[str, str]]:
    """Per-gate verdict-vocabulary registration for the live graph.

    Phase A's introspection helper joins this with `compiled_graph.get_graph()`
    to produce the verdict-labelled `PipelineTopology` consumed by the DAG view.
    """
    return _VERDICT_LABELS


def get_implicit_verdict_labels() -> Mapping[str, Mapping[str, str]]:
    """Verdict labels for implicit gates (nodes that set `final_status` directly).

    Today this covers only `classify` (LLM/deterministic cross-check
    disagreement). Phase D's DAG renderer draws these as implicit edges
    when the matching `exit_verdict` appears on the executed node.
    """
    return _IMPLICIT_VERDICT_LABELS


def _add_common_nodes_and_edges(graph: StateGraph) -> None:
    """Register all nodes and edges shared between normal and explain graphs.

    Sequence (post-2026-04-22 reorder): the proposal is fully
    materialised — recipe selected, gateway evidence resolved into
    enrichment_context, invocation params validated — BEFORE shadow
    audits it. Shadow gates *recipe execution and effects* per
    CLAUDE.md Guardrail #4; gateway READS are evidence acquisition,
    not execution, so they happen in the proposal phase. This keeps
    Verdict Pillar 1 (audit evidence captured at exception time)
    reachable for every record, including shadow-gated ones.

    Verdict Pillar 2: `build_analysis` sits on every terminal edge so
    the audit-bearing registry is enforced regardless of which
    lifecycle the record lands in (COMPLETE / BLOCKED / REJECTED /
    MANUAL_REVIEW_REQUIRED / FAIL_TO_HUMAN). Re-entry is cheap and
    the node no-ops on records it can't classify — no behavioural
    change for records outside the registry.

    Topology shared by live + explain:
        ingest → classify → load_skill → validate_circuit_breaker
          ├─[breach]→ build_analysis (FAIL_TO_HUMAN)
          └─[ok]→ select_recipe
               ├─[no recipe]→ build_analysis (FAIL_TO_HUMAN)
               └─[ok]→ resolve_dependencies
                    ├─[required-gw fail]→ build_analysis (FAIL_TO_HUMAN)
                    └─[ok]→ validate_types
                         ├─[invocation fail]→ build_analysis
                         └─[ok]→ shadow_audit
                              ├─[RED]→ build_analysis (BLOCKED, with audit evidence)
                              └─[YELLOW]→ build_analysis (MANUAL_REVIEW_REQUIRED, with audit evidence)
                              └─[GREEN]→ <variant: execute_recipe / explain_only>

    Each graph variant attaches the post-shadow continuation.
    """
    graph.add_node("ingest", nodes.ingest)
    graph.add_node("classify", nodes.classify)
    graph.add_node("load_skill", nodes.load_skill)
    graph.add_node("validate_circuit_breaker", nodes.validate_circuit_breaker)
    graph.add_node("select_recipe", nodes.select_recipe)
    graph.add_node("resolve_dependencies", nodes.resolve_dependencies)
    graph.add_node("validate_types", nodes.validate_types)
    graph.add_node("shadow_audit", nodes.shadow_audit)
    graph.add_node("build_analysis", nodes.build_analysis)

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "classify")
    graph.add_edge("classify", "load_skill")
    graph.add_edge("load_skill", "validate_circuit_breaker")
    graph.add_conditional_edges(
        "validate_circuit_breaker", route_after_gate,
        {"terminal": "build_analysis", "continue": "select_recipe"},
    )
    graph.add_conditional_edges(
        "select_recipe", route_after_gate,
        {"terminal": "build_analysis", "continue": "resolve_dependencies"},
    )
    graph.add_conditional_edges(
        "resolve_dependencies", route_after_gate,
        {"terminal": "build_analysis", "continue": "validate_types"},
    )
    graph.add_conditional_edges(
        "validate_types", route_after_gate,
        {"terminal": "build_analysis", "continue": "shadow_audit"},
    )
    # shadow_audit's continuation is variant-specific (execute_recipe
    # vs explain_only); each builder adds its own conditional edge.
    graph.add_edge("build_analysis", END)


@cache
def build_graph():
    """Build and compile the deterministic LangGraph state machine (normal execution).

    Live path post-shadow continuation:
        shadow_audit (continue/GREEN) → execute_recipe → apply_effects → build_analysis
    """
    graph = StateGraph(GraphState)
    _add_common_nodes_and_edges(graph)
    graph.add_node("execute_recipe", nodes.execute_recipe)
    graph.add_node("apply_effects", nodes.apply_effects)
    graph.add_conditional_edges(
        "shadow_audit", route_after_gate,
        {"terminal": "build_analysis", "continue": "execute_recipe"},
    )
    graph.add_edge("execute_recipe", "apply_effects")
    # Verdict Pillar 2: every GREEN-path terminus flows through
    # build_analysis so the audit-bearing registry is enforced even
    # on successful recipe executions.
    graph.add_edge("apply_effects", "build_analysis")
    return graph.compile()


@cache
def build_explain_graph():
    """Build the explain-mode graph.

    Identical to build_graph() except the post-shadow GREEN
    continuation runs explain_only (no recipe execution, no effects).
    Gateway READ dependencies run in the common section per the
    2026-04-22 reorder, so dry-runs see the same audit evidence the
    live path would see.

    Explain path post-shadow continuation:
        shadow_audit (continue/GREEN) → explain_only → build_analysis
    """
    graph = StateGraph(GraphState)
    _add_common_nodes_and_edges(graph)
    graph.add_node("explain_only", nodes.explain_only)
    graph.add_conditional_edges(
        "shadow_audit", route_after_gate,
        {"terminal": "build_analysis", "continue": "explain_only"},
    )
    # Verdict Pillar 2: explain-mode also runs through build_analysis
    # so dry-run traces exhibit the same AUDIT_CONTEXT_MISSING
    # behaviour as live executions.
    graph.add_edge("explain_only", "build_analysis")
    return graph.compile()


def run_graph(state: GraphState, *, explain_mode: bool | None = None) -> GraphState:
    """Invoke the graph and return a typed GraphState.

    Args:
        state: The initial GraphState.
        explain_mode: If True, force explain-mode graph. If None (default),
            check the ASOE_EXPLAIN_MODE env var. Passing the flag explicitly
            avoids mutating os.environ (thread-safe for concurrent requests).

    Phase 6 hardening checks (evaluated in order):
      1. Kill switch  — if ASOE_KILL_SWITCH=1, return FAIL_TO_HUMAN immediately
                        without running any node.
      2. Explain mode — use build_explain_graph() so execute_recipe is replaced
                        by explain_only.

    LangGraph 0.2+ serialises state to a plain dict at the invoke() boundary.
    This wrapper reconstructs the Pydantic model so callers always receive a
    typed result without having to deal with the serialisation artefact.

    Phase 5: after reconstruction, emit a structured trace record via the
    observability scaffold (stdlib logging — no langfuse package required).
    """
    # --- Phase 6: kill switch ---
    if is_kill_switch_active():
        return apply_kill_switch(state)

    # --- Phase 6: explain mode selects alternate graph ---
    use_explain = explain_mode if explain_mode is not None else is_explain_mode_active()
    compiled_graph = build_explain_graph() if use_explain else build_graph()

    result = compiled_graph.invoke(state)
    if isinstance(result, GraphState):
        final_state = result
    else:
        final_state = GraphState.model_validate(result)

    # Emit LangFuse-ready trace via stdlib logging (Phase 5 — no live integration).
    try:
        from observability.tracer import Tracer
        tracer = Tracer()
        tracer.emit(tracer.build_record(final_state))
    except (ImportError, AttributeError, ValueError, TypeError) as exc:  # pragma: no cover
        import logging
        logging.getLogger("asoe.observability").warning(
            "Tracer failed to emit record: %s", exc,
        )

    return final_state
