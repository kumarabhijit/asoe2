from __future__ import annotations

# Per-task constraint backend router.
#
# Resolution order for `get_constrained_backend(task)`:
#   1. If task ∈ ASOE_LLM_DISABLE_FOR (comma-separated runtime kill-list)
#      → DeterministicFallbackBackend
#   2. ASOE_LLM_PROVIDER_<TASK> per-task override (env)
#      Falls back to:
#   3. ASOE_LLM_PROVIDER global default (env)
#      Falls back to:
#   4. LOCAL_LLM_BACKEND_CLASS env (legacy SLM plug-in path; only when
#      provider resolves to "local")
#   5. USE_OUTLINES_BACKEND=1 → OutlinesConstrainedBackend (only when
#      provider resolves to "outlines")
#   6. DeterministicFallbackBackend
#
# Provider-to-backend mapping:
#   anthropic / openai / google / ollama / huggingface
#       → RemoteLLMBackend(provider_client)
#   outlines  → OutlinesConstrainedBackend
#   local     → load LOCAL_LLM_BACKEND_CLASS class
#   fallback  → DeterministicFallbackBackend
#
# Failure modes:
#   - UnknownProvider, ImportError, ProductionEgressBlocked,
#     RuntimeError (kill switch), ValueError (missing key),
#     NotImplementedError (V1 stub provider) — all caught and the
#     router falls closed to DeterministicFallbackBackend with a
#     structured warning.
#   - Operators MUST be able to switch providers at runtime via env
#     vars without a redeploy (matches kill-switch / explain-mode
#     pattern). The router reads env on every call (with a tiny
#     in-process cache that the test suite resets).
#
# Back-compat: `get_constrained_backend()` (no task arg) is retained
# and behaves as if task were unset — it resolves the global provider
# only and ignores per-task overrides. Existing call sites in
# orchestration/nodes.py / compliance/shadow.py / skills/intent_classifier.py
# keep working until S4 wires per-task routing through.

import importlib
import logging
import os
from typing import Any

from constraints.fallback_backend import DeterministicFallbackBackend
from contracts.policy import LLM_PROVIDER_DEFAULT, LLMProvider, LLMTask

logger = logging.getLogger("asoe.constraints")


_VALID_PROVIDERS: frozenset[str] = frozenset(
    LLMProvider.__args__  # type: ignore[attr-defined]
)
_VALID_TASKS: frozenset[str] = frozenset(LLMTask.__args__)  # type: ignore[attr-defined]


def _resolve_disabled_tasks(env: dict[str, str] | None = None) -> frozenset[str]:
    env = env if env is not None else os.environ
    raw = env.get("ASOE_LLM_DISABLE_FOR", "").strip()
    if not raw:
        return frozenset()
    return frozenset(t.strip() for t in raw.split(",") if t.strip() in _VALID_TASKS)


def _resolve_provider(task: LLMTask | None, env: dict[str, str] | None = None) -> str:
    """Resolve the provider for a given task with the precedence chain
    documented at the top of this module."""
    env = env if env is not None else os.environ
    if task is not None:
        per_task = env.get(f"ASOE_LLM_PROVIDER_{task.upper()}", "").strip()
        if per_task:
            return per_task
    glob = env.get("ASOE_LLM_PROVIDER", "").strip()
    if glob:
        return glob
    # Legacy USE_OUTLINES_BACKEND=1 short-circuit, retained for
    # back-compat with the V1-pre-LLM router contract.
    if env.get("USE_OUTLINES_BACKEND", "0") == "1":
        return "outlines"
    return LLM_PROVIDER_DEFAULT


def _build_remote_backend(provider: str) -> Any:
    """Build a RemoteLLMBackend wrapping the given provider client.

    Wraps the lazy build in a single try-except — every failure mode
    falls through to the deterministic fallback in the caller, with
    a structured log line so the operator can debug.
    """
    from constraints.llm_backend import RemoteLLMBackend  # noqa: PLC0415
    from llm.provider_factory import build_provider_client  # noqa: PLC0415

    client = build_provider_client(provider)
    return RemoteLLMBackend(provider_client=client)


def _build_local_backend() -> Any:
    """Load the sandbox SLM via LOCAL_LLM_BACKEND_CLASS env.

    Format: "module.path.ClassName". The class must implement the
    trio (classify_intent / propose_recipe / shadow_decision) over
    state: GraphState — same surface as DeterministicFallbackBackend.
    """
    spec = os.getenv("LOCAL_LLM_BACKEND_CLASS", "").strip()
    if not spec:
        raise ValueError(
            "ASOE_LLM_PROVIDER=local requires LOCAL_LLM_BACKEND_CLASS to be "
            "set to 'module.path.ClassName'."
        )
    module_path, _, class_name = spec.rpartition(".")
    if not module_path:
        raise ValueError(
            f"LOCAL_LLM_BACKEND_CLASS={spec!r} is not a fully-qualified "
            "class name."
        )
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls()


def _build_outlines_backend() -> Any:
    from constraints.outlines_backend import OutlinesConstrainedBackend  # noqa: PLC0415

    return OutlinesConstrainedBackend()


def _build_for_provider(provider: str) -> Any:
    """Dispatch on resolved provider, returning a backend that
    satisfies the trio interface. Caller catches ALL exceptions and
    falls through to DeterministicFallbackBackend — this function
    is allowed to be aggressive about validation."""
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider {provider!r}; expected one of "
            f"{sorted(_VALID_PROVIDERS)}."
        )
    if provider == "fallback":
        return DeterministicFallbackBackend()
    if provider == "outlines":
        return _build_outlines_backend()
    if provider == "local":
        return _build_local_backend()
    # anthropic / openai / google / ollama / huggingface — all go
    # through the unified RemoteLLMBackend.
    return _build_remote_backend(provider)


def get_constrained_backend(task: LLMTask | None = None) -> Any:
    """Return the env-driven constrained backend for `task`.

    Args:
        task: 'intent' | 'recipe' | 'shadow' | None. When None, this
            preserves the legacy contract (single backend, no per-task
            overrides) — used by call sites that haven't migrated to
            per-task routing yet.

    Returns:
        A backend instance implementing classify_intent /
        propose_recipe / shadow_decision over GraphState.

    The function NEVER raises — every failure mode falls closed to
    DeterministicFallbackBackend with a structured warning.
    """
    # 1. Runtime kill-by-task: an operator can flip a single task off
    # without re-deploying via ASOE_LLM_DISABLE_FOR=intent,recipe,shadow.
    if task is not None and task in _resolve_disabled_tasks():
        return DeterministicFallbackBackend()

    provider = _resolve_provider(task)
    try:
        return _build_for_provider(provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "constraint_router.fallback",
            extra={
                "task": task,
                "provider_requested": provider,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return DeterministicFallbackBackend()


__all__ = ("get_constrained_backend",)
