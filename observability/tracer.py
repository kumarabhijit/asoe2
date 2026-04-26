from __future__ import annotations

# Phase 5 — Observability scaffold
#
# TraceRecord: Pydantic model capturing all LangFuse-aligned fields for one
#              graph execution.  Fields mirror the LangFuse trace schema so a
#              future integration can forward records with minimal adaptation.
#
# Tracer: emits a structured JSON log via Python stdlib `logging`.
#         No langfuse package import.  Self-host friendly.
#
# LangFuse field alignment:
#   trace_id          → LangFuse trace.id
#   event_id          → LangFuse trace.input (order / event identifier)
#   skill_name        → LangFuse span "load_skill" → name
#   intent_selected   → LangFuse span "classify" → output
#   shadow_verdict    → LangFuse span "shadow_audit" → output.verdict
#   shadow_policy_hits → LangFuse span "shadow_audit" → output.policy_hits
#   recipe_name       → LangFuse span "select_recipe" → output
#   rag_chunks        → LangFuse span "rag_retrieval" → output
#   constrained_output_schemas → LangFuse metadata (Guidance/Outlines schemas used)
#   final_status      → LangFuse trace.output
#   explanation       → LangFuse trace.statusMessage

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from contracts.models import LLMCallTrace

logger = logging.getLogger("asoe.observability")


class TraceRecord(BaseModel):
    """Immutable snapshot of one graph execution — LangFuse-ready."""

    model_config = ConfigDict(extra="forbid")

    # Core correlation identifiers
    trace_id: str = Field(..., description="UUID propagated from ComplianceDecision through ExecutionLog")
    event_id: str = Field(..., description="order_id from the originating OrderEvent")

    # Skill / intent layer
    skill_name: Optional[str] = Field(None, description="SkillDocument.name selected by load_skill")
    intent_selected: Optional[str] = Field(None, description="Intent enum value after classify")

    # Compliance shadow layer
    shadow_verdict: Optional[str] = Field(None, description="GREEN | YELLOW | RED from ComplianceDecision")
    shadow_policy_hits: List[str] = Field(default_factory=list, description="Policy identifiers that fired")

    # Recipe layer
    recipe_name: Optional[str] = Field(None, description="Selected recipe filename or None")

    # RAG context
    rag_chunks: List[str] = Field(default_factory=list, description="RAG chunk identifiers attached to execution")

    # Constrained generation metadata
    constrained_output_schemas: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of layer → schema name used for constrained generation (e.g. intent→IntentDecision)",
    )

    # Gateway layer
    gateway_calls: List[str] = Field(
        default_factory=list,
        description="Gateway operations invoked (dependency resolutions + effect applications)",
    )

    # Override audit (Phase E) — populated when a human overrides the agent recommendation
    resolved_by: Optional[str] = Field(None, description="Identity of human who resolved the exception")
    resolved_action: Optional[str] = Field(None, description="Actual action taken (may differ from agent recommendation)")
    resolution_notes: Optional[str] = Field(None, description="Human-provided reason for override")

    # LLM provenance — V1 PR-1. One entry per remote-LLM trio call.
    # Empty list when the entire run was served by the deterministic
    # backend (legacy / kill-switch / explain-mode / fallback paths).
    # Each entry mirrors the LangFuse `generation` observation shape;
    # the LangFuse sink emits one generation span per entry.
    llm_calls: List[LLMCallTrace] = Field(
        default_factory=list,
        description="Per-call LLM telemetry: provider, model_id, request_id, "
                    "tokens, cost, fallback reason, cross-check disagreement.",
    )

    # Aggregate scalars derived from llm_calls — denormalised so
    # dashboards / cost queries don't have to walk the list. Updated
    # by Tracer.build_record from the live llm_calls slice.
    llm_total_input_tokens: int = Field(default=0)
    llm_total_output_tokens: int = Field(default=0)
    llm_total_cache_read_tokens: int = Field(default=0)
    llm_total_cache_creation_tokens: int = Field(default=0)
    llm_total_cost_usd_estimate: float = Field(default=0.0)
    llm_any_fallback: bool = Field(
        default=False,
        description="True if any LLM call in this run fell through to "
                    "the deterministic backend (provider error, circuit "
                    "open, budget hard-block, validation failure).",
    )
    llm_cross_check_disagreement: bool = Field(
        default=False,
        description="True if the intent classify call's LLM/deterministic "
                    "cross-check disagreed and the run was routed to "
                    "MANUAL_REVIEW_REQUIRED.",
    )

    # Terminal outcome
    final_status: Optional[str] = Field(None, description="TerminalStatus enum value")
    explanation: Optional[str] = Field(None, description="Human-readable explanation of the terminal decision")

    def to_json(self) -> str:
        """Serialise to a JSON string (LangFuse-compatible payload)."""
        return self.model_dump_json()


class Tracer:
    """Emits TraceRecord as structured JSON via stdlib logging.

    Usage:
        tracer = Tracer()
        record = tracer.build_record(state)
        tracer.emit(record)

    The emitted log record contains a ``trace`` key whose value is the
    full TraceRecord JSON object.  A LangFuse handler can be attached to
    the ``asoe.observability`` logger to forward records upstream.
    """

    def build_record(self, state: Any) -> TraceRecord:
        """Build a TraceRecord from a finalised GraphState.

        All fields are extracted defensively — a missing attribute yields
        None / empty list rather than raising, so the tracer never disrupts
        the main execution path.
        """
        # --- trace_id / event_id ---
        trace_id: str = ""
        event_id: str = ""

        if getattr(state, "execution_log", None) is not None:
            trace_id = state.execution_log.trace_id or ""
        if not trace_id and getattr(state, "shadow", None) is not None:
            trace_id = state.shadow.trace_id or ""
        event_id = getattr(getattr(state, "event", None), "order_id", "") or ""

        # --- skill layer ---
        skill_name: Optional[str] = None
        if getattr(state, "skill", None) is not None:
            skill_name = getattr(state.skill, "name", None)

        # --- intent layer ---
        # Intent.UNKNOWN means classify hasn't run yet; emit None for observability.
        intent_selected: Optional[str] = None
        raw_intent = getattr(state, "intent", None)
        if raw_intent is not None:
            value = raw_intent.value if hasattr(raw_intent, "value") else str(raw_intent)
            if value != "UNKNOWN":
                intent_selected = value

        # --- shadow layer ---
        shadow_verdict: Optional[str] = None
        shadow_policy_hits: List[str] = []
        if getattr(state, "shadow", None) is not None:
            raw_verdict = state.shadow.status
            shadow_verdict = raw_verdict.value if hasattr(raw_verdict, "value") else str(raw_verdict)
            shadow_policy_hits = list(state.shadow.policy_hits or [])

        # --- recipe layer ---
        recipe_name: Optional[str] = None
        if getattr(state, "execution_log", None) is not None:
            recipe_name = getattr(state.execution_log, "recipe_name", None)
        if recipe_name is None:
            recipe_name = getattr(state, "selected_recipe", None)

        # --- RAG chunks ---
        rag_chunks: List[str] = []
        if getattr(state, "execution_log", None) is not None:
            rag_chunks = list(getattr(state.execution_log, "rag_chunks", None) or [])
        if not rag_chunks:
            rag_context = getattr(state, "rag_context", None)
            if rag_context is not None:
                rag_chunks = list(getattr(rag_context, "chunks", None) or [])

        # --- constrained output schemas ---
        constrained_output_schemas: Dict[str, str] = {}
        if getattr(state, "execution_log", None) is not None:
            constrained_output_schemas = dict(state.execution_log.constrained_outputs or {})

        # --- gateway calls ---
        gateway_calls: List[str] = []
        for resp in getattr(state, "effect_results", None) or []:
            gw_name = getattr(resp, "gateway_name", "?")
            gw_op = getattr(resp, "operation", "?")
            gw_status = getattr(resp, "status", "?")
            gateway_calls.append(f"{gw_name}/{gw_op}:{gw_status}")
        for key in getattr(state, "enrichment_context", None) or {}:
            gateway_calls.append(f"dep:{key}:resolved")

        # --- override audit ---
        resolved_by: Optional[str] = None
        resolved_action: Optional[str] = None
        resolution_notes: Optional[str] = None
        if getattr(state, "execution_log", None) is not None:
            resolved_by = getattr(state.execution_log, "resolved_by", None)
            resolved_action = getattr(state.execution_log, "resolved_action", None)
            resolution_notes = getattr(state.execution_log, "resolution_notes", None)

        # --- terminal outcome ---
        final_status: Optional[str] = None
        if getattr(state, "final_status", None) is not None:
            raw_status = state.final_status
            final_status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)

        explanation: Optional[str] = getattr(state, "explanation", None)

        # --- LLM provenance ---
        # state.llm_call_traces is the authoritative list — populated
        # by orchestration/nodes.py::_drain_llm_trace after each trio
        # call. Empty when the run never engaged a remote LLM (default
        # / kill-switch / explain-mode / fallback paths).
        llm_calls: List[LLMCallTrace] = []
        raw_calls = getattr(state, "llm_call_traces", None) or []
        for call in raw_calls:
            if isinstance(call, LLMCallTrace):
                llm_calls.append(call)

        llm_total_input_tokens = sum(c.input_tokens for c in llm_calls)
        llm_total_output_tokens = sum(c.output_tokens for c in llm_calls)
        llm_total_cache_read_tokens = sum(c.cache_read_input_tokens for c in llm_calls)
        llm_total_cache_creation_tokens = sum(
            c.cache_creation_input_tokens for c in llm_calls
        )
        llm_total_cost_usd_estimate = sum(c.cost_usd_estimate for c in llm_calls)
        llm_any_fallback = any(c.fallback_to_deterministic for c in llm_calls)
        llm_cross_check_disagreement = any(
            c.cross_check_disagreement is True for c in llm_calls
        )

        return TraceRecord(
            trace_id=trace_id,
            event_id=event_id,
            skill_name=skill_name,
            intent_selected=intent_selected,
            shadow_verdict=shadow_verdict,
            shadow_policy_hits=shadow_policy_hits,
            recipe_name=recipe_name,
            rag_chunks=rag_chunks,
            constrained_output_schemas=constrained_output_schemas,
            gateway_calls=gateway_calls,
            resolved_by=resolved_by,
            resolved_action=resolved_action,
            resolution_notes=resolution_notes,
            llm_calls=llm_calls,
            llm_total_input_tokens=llm_total_input_tokens,
            llm_total_output_tokens=llm_total_output_tokens,
            llm_total_cache_read_tokens=llm_total_cache_read_tokens,
            llm_total_cache_creation_tokens=llm_total_cache_creation_tokens,
            llm_total_cost_usd_estimate=round(llm_total_cost_usd_estimate, 6),
            llm_any_fallback=llm_any_fallback,
            llm_cross_check_disagreement=llm_cross_check_disagreement,
            final_status=final_status,
            explanation=explanation,
        )

    def emit(self, record: TraceRecord) -> None:
        """Emit the TraceRecord as a structured JSON log entry.

        The log message contains a ``trace`` key so log aggregators and a
        future LangFuse handler can extract the payload without string parsing.

        If LangFuse is configured (env vars set + package installed), the
        record is also forwarded to LangFuse.  Forwarding failures are
        logged but never block execution.
        """
        payload = {"trace": record.model_dump(mode="json")}
        logger.info(json.dumps(payload))

        # Optional LangFuse forwarding — additive, never blocks.
        try:
            from observability.langfuse_sink import forward
            forward(record)
        except Exception as exc:
            logger.debug("LangFuse sink unavailable: %s", exc)
