from __future__ import annotations

import importlib
import os

from constraints.fallback_backend import DeterministicFallbackBackend


def get_constrained_backend():
    # Sandbox / custom backend — accepts a fully-qualified "module.ClassName" string.
    # Allows tests/sandbox/llm/local_backend.py to be injected without a hard
    # production-code dependency on the sandbox module.
    custom_class = os.getenv("LOCAL_LLM_BACKEND_CLASS", "").strip()
    if custom_class:
        module_path, class_name = custom_class.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)()

    use_outlines = os.getenv("USE_OUTLINES_BACKEND", "0") == "1"
    if use_outlines:
        # Lazy import: outlines + transformers are heavy optional dependencies.
        # Only pulled in when USE_OUTLINES_BACKEND=1 is explicitly set.
        from constraints.outlines_backend import OutlinesConstrainedBackend  # noqa: PLC0415
        return OutlinesConstrainedBackend()
    return DeterministicFallbackBackend()
