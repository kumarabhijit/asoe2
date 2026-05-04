from __future__ import annotations

# LangFuse Sink — optional forwarder for TraceRecord → LangFuse.
#
# Design decisions (per expert panel recommendations):
#   - Optional dependency: no-op when langfuse is not installed or not configured.
#   - Failure isolation: all LangFuse errors are caught and logged; never blocks execution.
#   - Stdlib logging remains the primary emit channel; LangFuse is additive.
#   - Configured via env vars: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST.
#   - Secret management: keys should be injected via Azure Key Vault CSI in production.
#   - Supports both langfuse v2 (trace/span/score) and v4+ (start_observation/create_score).
#
# Span coverage. The sink emits one span per LangGraph node that ran,
# derived from observable signals on the TraceRecord. Mapping (per
# architecture_v4.md §5.1 graph topology):
#
#   ingest                     ←  event_id
#   classify                   ←  intent_selected (LLM children: task=intent)
#   load_skill                 ←  skill_name
#   validate_circuit_breaker   ←  always when graph_ran
#   select_recipe              ←  recipe_name (LLM children: task=recipe)
#   resolve_dependencies       ←  gateway_calls entries with "dep:" prefix
#   validate_types             ←  recipe_name
#   shadow_audit               ←  shadow_verdict (LLM children: task=shadow)
#   execute_recipe             ←  recipe_name AND shadow_verdict == GREEN
#   apply_effects              ←  gateway_calls entries without "dep:" prefix
#   build_analysis             ←  final_status (Pillar 2 composer; always at terminal)
#
# LLM `generation` observations are attached as **children of their owning
# step span** (intent → classify, recipe → select_recipe, shadow →
# shadow_audit). LangFuse renders the parent span's child list with the
# native cost/usage UI for generations, so an operator opening "classify"
# sees "what did the LLM return for intent?" inline. Orphan generations
# (LLM call ran but the owning step span gated out) attach to the root.

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("asoe.observability")

# Sentinel: None means "not yet attempted"; False means "unavailable".
_langfuse_client: Any = None
_initialised: bool = False


def _get_client() -> Any:
    """Lazily initialise the LangFuse client.

    Returns the client instance, or None if langfuse is not installed or
    the required env vars are missing.  Initialisation is attempted once;
    subsequent calls return the cached result.
    """
    global _langfuse_client, _initialised

    if _initialised:
        return _langfuse_client

    _initialised = True

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")

    if not public_key or not secret_key:
        logger.debug("LangFuse sink disabled: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
        _langfuse_client = None
        return None

    try:
        from langfuse import Langfuse  # type: ignore[import-untyped]

        host = os.environ.get("LANGFUSE_HOST", None)
        kwargs: dict[str, str] = {
            "public_key": public_key,
            "secret_key": secret_key,
        }
        if host:
            kwargs["host"] = host

        _langfuse_client = Langfuse(**kwargs)
        logger.info("LangFuse sink initialised (host=%s)", host or "cloud default")
    except ImportError:
        logger.debug("LangFuse sink disabled: langfuse package not installed")
        _langfuse_client = None
    except Exception as exc:
        logger.warning("LangFuse sink init failed: %s", exc)
        _langfuse_client = None

    return _langfuse_client


def reset_client() -> None:
    """Reset the cached client.  Used by tests and config changes."""
    global _langfuse_client, _initialised
    _langfuse_client = None
    _initialised = False


def _is_v2(client: Any) -> bool:
    """Return True if the client exposes the v2 API (trace/span/score)."""
    return hasattr(client, "trace") and not hasattr(client, "start_observation")


def _root_payload(record: Any) -> dict:
    """Build the common root trace/observation payload from a TraceRecord."""
    return {
        "name": "asoe-graph-execution",
        "input": {"event_id": record.event_id},
        "output": {"final_status": record.final_status, "explanation": record.explanation},
        "metadata": {
            "constrained_output_schemas": record.constrained_output_schemas,
            "gateway_calls": record.gateway_calls,
            "rag_chunks": record.rag_chunks,
            "llm_total_input_tokens": record.llm_total_input_tokens,
            "llm_total_output_tokens": record.llm_total_output_tokens,
            "llm_total_cost_usd_estimate": record.llm_total_cost_usd_estimate,
            "llm_any_fallback": record.llm_any_fallback,
            "llm_cross_check_disagreement": record.llm_cross_check_disagreement,
        },
    }


def _backend_for(task: str, record: Any) -> str:
    """Return '<provider>:<model_id>' if a remote LLM served this task,
    else 'deterministic'. Surfaces backend choice on classify /
    select_recipe / shadow_audit spans even when no LLM call fired,
    so operators can tell at a glance which tier served the run."""
    for call in getattr(record, "llm_calls", None) or []:
        if call.task == task:
            return f"{call.provider}:{call.model_id or '(fallback)'}"
    return "deterministic"


def _graph_ran(record: Any) -> bool:
    """Did the graph traverse beyond ingest? Used to gate the
    'always-runs' nodes (ingest, validate_circuit_breaker,
    build_analysis) on a record that actually came from a graph run
    rather than a hand-built minimal stub."""
    return bool(
        record.final_status
        or record.intent_selected
        or record.skill_name
        or record.shadow_verdict
        or record.recipe_name
    )


def _span_entries(record: Any) -> list[dict]:
    """Build the list of child span kwargs from a TraceRecord.

    Each entry is a dict shaped for LangFuse `span()` / `start_observation()`
    plus a private `_llm_tasks` key listing the LLMCallTrace.task names
    that should be nested as `generation`-typed children of this span.
    """
    spans: list[dict] = []
    schemas = record.constrained_output_schemas or {}
    graph_ran = _graph_ran(record)

    if graph_ran and record.event_id:
        spans.append({
            "name": "ingest",
            "output": {"event_id": record.event_id},
            "_llm_tasks": [],
        })

    if record.intent_selected:
        spans.append({
            "name": "classify",
            "output": {"intent": record.intent_selected},
            "metadata": {
                "constrained_by": schemas.get("intent"),
                "backend_used": _backend_for("intent", record),
                "cross_check_disagreement": record.llm_cross_check_disagreement,
            },
            "level": "WARNING" if record.llm_cross_check_disagreement else "DEFAULT",
            "_llm_tasks": ["intent"],
        })

    if record.skill_name:
        spans.append({
            "name": "load_skill",
            "output": {"skill_name": record.skill_name},
            "_llm_tasks": [],
        })

    if graph_ran:
        # Circuit breaker — ran on every traversal. A FAIL_TO_HUMAN
        # without any subsequent shadow/recipe signal is a likely
        # breaker breach (also covers no-recipe / required-gw / type
        # validation failures, which are all FAIL_TO_HUMAN class).
        cb_breached = (
            record.final_status == "FAIL_TO_HUMAN"
            and not record.recipe_name
            and not record.shadow_verdict
        )
        spans.append({
            "name": "validate_circuit_breaker",
            "output": {"breached": cb_breached},
            "level": "WARNING" if cb_breached else "DEFAULT",
            "_llm_tasks": [],
        })

    if record.recipe_name:
        spans.append({
            "name": "select_recipe",
            "output": {"recipe_name": record.recipe_name},
            "metadata": {
                "constrained_by": schemas.get("recipe"),
                "backend_used": _backend_for("recipe", record),
            },
            "_llm_tasks": ["recipe"],
        })

    # gateway_calls is a flat list of "dep:<key>:resolved" (dependency
    # READS) and "<gw>/<op>:<status>" (effect WRITES). Split so each
    # node owns its half.
    dep_calls = [g for g in (record.gateway_calls or []) if g.startswith("dep:")]
    effect_calls = [g for g in (record.gateway_calls or []) if not g.startswith("dep:")]

    if dep_calls:
        spans.append({
            "name": "resolve_dependencies",
            "output": {"dependencies_resolved": len(dep_calls)},
            "metadata": {"resolved": dep_calls},
            "_llm_tasks": [],
        })

    if record.recipe_name:
        spans.append({
            "name": "validate_types",
            "output": {"recipe_name": record.recipe_name},
            "_llm_tasks": [],
        })

    if record.shadow_verdict:
        spans.append({
            "name": "shadow_audit",
            "output": {"verdict": record.shadow_verdict, "policy_hits": record.shadow_policy_hits},
            "metadata": {
                "constrained_by": schemas.get("shadow"),
                "backend_used": _backend_for("shadow", record),
            },
            "level": "WARNING" if record.shadow_verdict != "GREEN" else "DEFAULT",
            "_llm_tasks": ["shadow"],
        })

    # execute_recipe only runs on GREEN — YELLOW/RED halt at shadow.
    if record.recipe_name and record.shadow_verdict == "GREEN":
        spans.append({
            "name": "execute_recipe",
            "output": {
                "recipe_name": record.recipe_name,
                "resolved_action": record.resolved_action,
            },
            "metadata": {
                "resolved_by": record.resolved_by,
                "resolution_notes": record.resolution_notes,
            },
            "_llm_tasks": [],
        })

    if effect_calls:
        spans.append({
            "name": "apply_effects",
            "output": {"effects_applied": len(effect_calls)},
            "metadata": {"effects": effect_calls},
            "_llm_tasks": [],
        })

    if graph_ran and record.final_status:
        spans.append({
            "name": "build_analysis",
            "output": {
                "final_status": record.final_status,
                "explanation": record.explanation,
            },
            "_llm_tasks": [],
        })

    return spans


def _generation_kwargs(call: Any) -> dict:
    """Map a single LLMCallTrace to LangFuse `generation` kwargs.

    Prompt content is never included — only hashes — per the PII guard
    in CLAUDE.md §6.
    """
    usage = {
        "input": call.input_tokens,
        "output": call.output_tokens,
        "total": call.input_tokens + call.output_tokens,
        "unit": "TOKENS",
    }
    metadata: dict[str, Any] = {
        "provider": call.provider,
        "request_id": call.request_id,
        "prompt_hash": call.prompt_hash,
        "tool_call_hash": call.tool_call_hash,
        "skill_md_version": call.skill_md_version,
        "cache_read_input_tokens": call.cache_read_input_tokens,
        "cache_creation_input_tokens": call.cache_creation_input_tokens,
        "latency_ms": call.latency_ms,
        "cost_usd_estimate": call.cost_usd_estimate,
        "stop_reason": call.stop_reason,
        "fallback_to_deterministic": call.fallback_to_deterministic,
        "fallback_reason": call.fallback_reason,
    }
    if call.cross_check_disagreement is not None:
        metadata["cross_check_disagreement"] = call.cross_check_disagreement
        metadata["cross_check_llm_intent"] = call.cross_check_llm_intent
        metadata["cross_check_deterministic_intent"] = call.cross_check_deterministic_intent

    output: dict[str, Any] = {
        "tool_call_hash": call.tool_call_hash,
        "stop_reason": call.stop_reason,
    }
    if call.cross_check_disagreement:
        output["cross_check"] = "DISAGREEMENT"

    level = "DEFAULT"
    if call.fallback_to_deterministic or call.cross_check_disagreement:
        level = "WARNING"

    entry: dict[str, Any] = {
        "name": f"llm.{call.task}",
        "model": call.model_id or "(fallback)",
        "input": {"prompt_hash": call.prompt_hash},
        "output": output,
        "usage": usage,
        "metadata": metadata,
        "level": level,
    }
    if call.fallback_reason:
        entry["status_message"] = call.fallback_reason
    return entry


def _llm_calls_by_task(record: Any) -> dict[str, list[dict]]:
    """Group generation kwargs by LLMCallTrace.task. A single graph run
    may carry multiple calls for the same task (retry / cross-check)."""
    grouped: dict[str, list[dict]] = {}
    for call in getattr(record, "llm_calls", None) or []:
        grouped.setdefault(call.task, []).append(_generation_kwargs(call))
    return grouped


def _strip_private(span_kwargs: dict) -> dict:
    """Drop the private `_llm_tasks` key before sending to the SDK —
    it's used only by the forward functions to nest generations."""
    return {k: v for k, v in span_kwargs.items() if not k.startswith("_")}


def _forward_v2(client: Any, record: Any) -> bool:
    """Forward using langfuse v2 API: client.trace() → trace.span() /
    span.generation() / trace.score()."""
    trace = client.trace(id=record.trace_id or None, **_root_payload(record))

    gens_by_task = _llm_calls_by_task(record)

    for span_kwargs in _span_entries(record):
        owned_tasks = span_kwargs.get("_llm_tasks", [])
        span = trace.span(**_strip_private(span_kwargs))
        for task in owned_tasks:
            for gen_kwargs in gens_by_task.pop(task, []):
                span.generation(**gen_kwargs)

    # Orphan generations: LLM call ran but the owning step span gated
    # out (e.g. classify backend errored before intent_selected was
    # set). Attach to the trace root rather than dropping them.
    for orphan_list in gens_by_task.values():
        for gen_kwargs in orphan_list:
            trace.generation(**gen_kwargs)

    if record.final_status:
        trace.score(
            name="terminal_status",
            value=1.0 if record.final_status == "COMPLETE" else 0.0,
            comment=record.final_status,
        )

    return True


def _forward_v4(client: Any, record: Any) -> bool:
    """Forward using langfuse v4+ API: start_observation / create_score."""
    raw_id = record.trace_id or ""
    trace_id = raw_id.replace("-", "").lower() if raw_id else None
    trace_ctx = {"trace_id": trace_id} if trace_id else None

    root = client.start_observation(trace_context=trace_ctx, **_root_payload(record))

    gens_by_task = _llm_calls_by_task(record)

    for span_kwargs in _span_entries(record):
        owned_tasks = span_kwargs.get("_llm_tasks", [])
        child = root.start_observation(**_strip_private(span_kwargs))
        for task in owned_tasks:
            for gen_kwargs in gens_by_task.pop(task, []):
                grandchild = child.start_observation(as_type="generation", **gen_kwargs)
                grandchild.end()
        child.end()

    for orphan_list in gens_by_task.values():
        for gen_kwargs in orphan_list:
            orphan = root.start_observation(as_type="generation", **gen_kwargs)
            orphan.end()

    root.end()

    if record.final_status and trace_id:
        client.create_score(
            trace_id=trace_id,
            name="terminal_status",
            value=1.0 if record.final_status == "COMPLETE" else 0.0,
            comment=record.final_status,
        )

    return True


def forward(record: Any) -> bool:
    """Forward a TraceRecord to LangFuse.

    Creates a LangFuse trace with spans mirroring the graph pipeline stages.
    Returns True if the record was forwarded, False otherwise.

    Auto-detects langfuse SDK version and uses the appropriate API.
    """
    client = _get_client()
    if client is None:
        return False

    try:
        if _is_v2(client):
            return _forward_v2(client, record)
        return _forward_v4(client, record)
    except Exception as exc:
        logger.warning("LangFuse forward failed: %s", exc)
        return False


def flush() -> None:
    """Flush pending LangFuse events.  No-op if client is not active."""
    client = _get_client()
    if client is not None:
        try:
            client.flush()
        except Exception as exc:
            logger.warning("LangFuse flush failed: %s", exc)
