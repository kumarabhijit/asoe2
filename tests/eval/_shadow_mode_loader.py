"""Shadow-mode thresholds loader (PARITY-6).

Plain helper that parses ``shadow_mode_thresholds.yaml`` once and
returns the dict. Kept as a private test helper to keep the loader
out of the production import graph.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


_THRESHOLDS = Path(__file__).resolve().parent / "shadow_mode_thresholds.yaml"


@lru_cache(maxsize=1)
def load_thresholds() -> dict:
    """Return the shadow-mode threshold tree. Cached for the test session."""
    return yaml.safe_load(_THRESHOLDS.read_text())
