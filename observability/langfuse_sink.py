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
        },
    }


def _span_entries(record: Any) -> list[dict]:
    """Build the list of child span kwargs from a TraceRecord.

    Each entry is a dict with keys: name, output, and optionally level.
    Only spans whose guard field is non-None are included.
    """
    spans: list[dict] = []
    if record.intent_selected:
        spans.append({"name": "classify", "output": {"intent": record.intent_selected}})
    if record.skill_name:
        spans.append({"name": "load_skill", "output": {"skill_name": record.skill_name}})
    if record.shadow_verdict:
        spans.append({
            "name": "shadow_audit",
            "output": {"verdict": record.shadow_verdict, "policy_hits": record.shadow_policy_hits},
            "level": "WARNING" if record.shadow_verdict != "GREEN" else "DEFAULT",
        })
    if record.recipe_name:
        spans.append({"name": "execute_recipe", "output": {"recipe_name": record.recipe_name}})
    return spans


def _generation_entries(record: Any) -> list[dict]:
    """Build LangFuse `generation`-shaped kwargs from llm_calls.

    LangFuse models LLM calls as a distinct observation type
    (`generation`) with native fields for `model`, `input`/`output`,
    `usage` (token counts), and `metadata`. Emitting one generation
    per LLMCallTrace makes the LangFuse UI render token totals,
    cost estimates, and cache-hit rates correctly per call.

    Each entry is keyword-args ready for v2 `trace.generation(**kw)`
    or v4 `start_observation(as_type='generation', **kw)`.

    Fields populated on every entry:
      - name           : 'llm.<task>' (intent / recipe / shadow)
      - model          : resolved model_id from the provider response
                         (empty string on fallback paths — LangFuse
                         tolerates and just shows no model badge)
      - input          : truncated rendering hints (no prompt content;
                         only hashes — provider prompts can include
                         PII per CLAUDE.md §6 and never enter LangFuse)
      - output         : tool_call_hash + cross-check signal when
                         present
      - usage          : {prompt_tokens, completion_tokens, total_tokens}
                         — LangFuse v2 native field shape that drives
                         the model-pricing cost calculator
      - metadata       : provider, request_id, prompt_hash, fallback
                         flags, latency, cost_usd_estimate, cache hits
      - level          : 'WARNING' on fallback_to_deterministic /
                         cross_check_disagreement, else 'DEFAULT'
      - status_message : fallback_reason when present
    """
    entries: list[dict] = []
    for call in getattr(record, "llm_calls", None) or []:
        usage = {
            "input": call.input_tokens,
            "output": call.output_tokens,
            "total": call.input_tokens + call.output_tokens,
            "unit": "TOKENS",
        }
        # Cache hits are not part of LangFuse's standard usage shape;
        # carry them in metadata so dashboards can chart them.
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

        # Output: never the prompt body. Hashes + signals only.
        output: dict[str, Any] = {
            "tool_call_hash": call.tool_call_hash,
            "stop_reason": call.stop_reason,
        }
        if call.cross_check_disagreement:
            output["cross_check"] = "DISAGREEMENT"

        # Level. Fallback or disagreement → WARNING so dashboards
        # surface the run as needing attention.
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
        entries.append(entry)
    return entries


def _forward_v2(client: Any, record: Any) -> bool:
    """Forward using langfuse v2 API: client.trace() → trace.span() /
    trace.generation() / trace.score()."""
    trace = client.trace(id=record.trace_id or None, **_root_payload(record))

    for span_kwargs in _span_entries(record):
        trace.span(**span_kwargs)

    # Per-LLM-call generations — LangFuse renders these with the
    # native cost/usage UI when token counts + model are present.
    for gen_kwargs in _generation_entries(record):
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

    for span_kwargs in _span_entries(record):
        child = root.start_observation(**span_kwargs)
        child.end()

    # Per-LLM-call generations — v4 uses `as_type="generation"` to
    # tag the observation as an LLM call so the UI surfaces it
    # under the model/cost views.
    for gen_kwargs in _generation_entries(record):
        child = root.start_observation(as_type="generation", **gen_kwargs)
        child.end()

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
