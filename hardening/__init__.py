from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes"}


def is_env_truthy(var_name: str, default: str = "0") -> bool:
    """Return True if the named env var is set to a truthy value (1/true/yes)."""
    return os.getenv(var_name, default).strip().lower() in _TRUTHY


from .kill_switch import is_kill_switch_active, apply_kill_switch  # noqa: E402
from .explain_mode import is_explain_mode_active  # noqa: E402

__all__ = [
    "is_env_truthy",
    "is_kill_switch_active",
    "apply_kill_switch",
    "is_explain_mode_active",
]
