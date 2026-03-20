from __future__ import annotations

import importlib
import logging
import os

from constraints.fallback_backend import DeterministicFallbackBackend

logger = logging.getLogger("asoe.constraints")


def get_constrained_backend():
    """Return the env-driven constrained backend with fallback chain.

    Resolution order:
      1. LOCAL_LLM_BACKEND_CLASS env var (sandbox / custom backend)
      2. USE_OUTLINES_BACKEND=1 → OutlinesConstrainedBackend
      3. DeterministicFallbackBackend (always available)

    If (1) or (2) fails to initialise, degrades gracefully to (3)
    with a warning log so the failure is visible but not fatal.
    """
    # Sandbox / custom backend — accepts a fully-qualified "module.ClassName" string.
    custom_class = os.getenv("LOCAL_LLM_BACKEND_CLASS", "").strip()
    if custom_class:
        try:
            module_path, class_name = custom_class.rsplit(".", 1)
            module = importlib.import_module(module_path)
            return getattr(module, class_name)()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Custom backend %s failed to load; falling back to DeterministicFallbackBackend: %s",
                custom_class,
                exc,
            )
            return DeterministicFallbackBackend()

    use_outlines = os.getenv("USE_OUTLINES_BACKEND", "0") == "1"
    if use_outlines:
        try:
            # Lazy import: outlines + transformers are heavy optional dependencies.
            from constraints.outlines_backend import OutlinesConstrainedBackend  # noqa: PLC0415
            return OutlinesConstrainedBackend()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OutlinesConstrainedBackend failed to initialise; falling back to "
                "DeterministicFallbackBackend: %s",
                exc,
            )
            return DeterministicFallbackBackend()

    return DeterministicFallbackBackend()
