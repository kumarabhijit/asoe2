"""PARITY-6 — canary traffic-split helper.

Per-connector real-vs-stub routing keyed on the case_id so the same
case always routes the same way. The plan calls for a phased rollout:
5% → 25% → 100% over a week per connector.

Determinism is the contract: a recipe that runs the same case twice
must see the same canary decision, otherwise the audit-bearing diff
log accumulates spurious entries on retries.

Usage::

    if is_canary_eligible(case_id=case_id, pct=current_canary_pct(connector)):
        result = real_gateway.call(...)
    else:
        result = stub_gateway.call(...)
"""

from __future__ import annotations

import hashlib
import os


def _hash_to_unit(case_id: str) -> float:
    """Map a case_id to a deterministic float in [0, 1).

    SHA-256 the case_id, take the first 8 bytes as a 64-bit unsigned
    int, divide by 2^64 → uniform-ish distribution.
    """
    if not case_id:
        return 0.0
    digest = hashlib.sha256(case_id.encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big", signed=False)
    return n / (1 << 64)


def is_canary_eligible(*, case_id: str, pct: float) -> bool:
    """``True`` iff this case_id falls into the first ``pct`` slice of
    the hash distribution.

    ``pct`` is in [0.0, 1.0]. Values outside that range are clamped.
    """
    if pct <= 0.0:
        return False
    if pct >= 1.0:
        return True
    return _hash_to_unit(case_id) < pct


def current_canary_pct(connector: str) -> float:
    """Return the active canary percentage for ``connector``, sourced
    from ``ASOE_CANARY_PCT_{connector_upper}`` env var.

    Default 0.0 — until the operator opts in by setting the env var,
    no real traffic is routed. The Container App revision restart
    after a ``set-secrets.sh`` update applies the new value.
    """
    env_var = f"ASOE_CANARY_PCT_{connector.upper().replace('-', '_')}"
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return 0.0
    try:
        v = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, min(1.0, v))
