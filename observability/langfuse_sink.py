from __future__ import annotations

# LangFuse Sink — optional forwarder for TraceRecord → LangFuse.
#
# Design decisions (per expert panel recommendations):
#   - Optional dependency: no-op when langfuse is not installed or not configured.
#   - Failure isolation: all LangFuse errors are caught and logged; never blocks execution.
#   - Stdlib logging remains the primary emit channel; LangFuse is additive.
#   - Configured via env vars: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST.
#   - Secret management: keys should be injected via Azure Key Vault CSI in production.

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


def forward(record: Any) -> bool:
    """Forward a TraceRecord to LangFuse.

    Creates a LangFuse trace with spans mirroring the graph pipeline stages.
    Returns True if the record was forwarded, False otherwise.

    Compatible with langfuse v4+ API (start_observation / create_score).
    """
    client = _get_client()
    if client is None:
        return False

    try:
        # LangFuse v4 requires 32 lowercase hex chars (no dashes).
        raw_id = record.trace_id or ""
        trace_id = raw_id.replace("-", "").lower() if raw_id else None
        trace_ctx = {"trace_id": trace_id} if trace_id else None

        # Root span — represents one run_graph() execution.
        root = client.start_observation(
            trace_context=trace_ctx,
            name="asoe-graph-execution",
            input={"event_id": record.event_id},
            output={"final_status": record.final_status, "explanation": record.explanation},
            metadata={
                "constrained_output_schemas": record.constrained_output_schemas,
                "gateway_calls": record.gateway_calls,
                "rag_chunks": record.rag_chunks,
            },
        )

        # Child span: intent classification
        if record.intent_selected:
            child = root.start_observation(
                name="classify",
                output={"intent": record.intent_selected},
            )
            child.end()

        # Child span: skill loading
        if record.skill_name:
            child = root.start_observation(
                name="load_skill",
                output={"skill_name": record.skill_name},
            )
            child.end()

        # Child span: compliance shadow audit
        if record.shadow_verdict:
            child = root.start_observation(
                name="shadow_audit",
                output={
                    "verdict": record.shadow_verdict,
                    "policy_hits": record.shadow_policy_hits,
                },
                level="WARNING" if record.shadow_verdict != "GREEN" else "DEFAULT",
            )
            child.end()

        # Child span: recipe execution
        if record.recipe_name:
            child = root.start_observation(
                name="execute_recipe",
                output={"recipe_name": record.recipe_name},
            )
            child.end()

        root.end()

        # Score: terminal status (allows LangFuse dashboard filtering)
        if record.final_status and trace_id:
            client.create_score(
                trace_id=trace_id,
                name="terminal_status",
                value=1.0 if record.final_status == "COMPLETE" else 0.0,
                comment=record.final_status,
            )

        return True

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
