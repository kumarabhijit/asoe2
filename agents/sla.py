"""ADR-038 Phase H.7 — SLA per-customer-tier policy loader + applier.

Reads ``knowledge/policy/sla_per_customer_tier.yaml`` (the L0 policy
artefact). The L4 harness calls ``stamp_sla_deadline()`` at case open
to populate ``OrderCase.sla_deadline``.

Pure module: no I/O beyond reading the policy YAML once at module
load. The policy loader is cached; tests can call ``reload_policy()``
to invalidate and re-read.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

import yaml


POLICY_PATH = Path("knowledge/policy/sla_per_customer_tier.yaml")


@dataclass(frozen=True)
class SlaPolicy:
    """One row in the SLA table."""
    sla_hours: int
    description: str = ""


@dataclass
class SlaPolicySet:
    """All tiers + the default fallback."""
    default_sla_hours: int
    tiers: Dict[str, SlaPolicy]

    def hours_for_tier(self, tier_name: Optional[str]) -> int:
        if not tier_name:
            return self.default_sla_hours
        policy = self.tiers.get(tier_name)
        if policy is None:
            return self.default_sla_hours
        return policy.sla_hours


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


_lock = threading.RLock()
_cached_policy: Optional[SlaPolicySet] = None


def _load_from_disk() -> SlaPolicySet:
    if not POLICY_PATH.exists():
        # Defensive fallback for environments where the L0 bundle
        # isn't present (e.g. an isolated test fixture).
        return SlaPolicySet(default_sla_hours=48, tiers={})
    with POLICY_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return SlaPolicySet(default_sla_hours=48, tiers={})
    default_hours = int(data.get("default_sla_hours", 48))
    raw_tiers = data.get("tiers") or {}
    tiers: Dict[str, SlaPolicy] = {}
    if isinstance(raw_tiers, dict):
        for name, entry in raw_tiers.items():
            if not isinstance(entry, dict):
                continue
            try:
                hours = int(entry.get("sla_hours", default_hours))
            except (TypeError, ValueError):
                continue
            tiers[str(name)] = SlaPolicy(
                sla_hours=hours,
                description=str(entry.get("description") or ""),
            )
    return SlaPolicySet(default_sla_hours=default_hours, tiers=tiers)


def get_policy() -> SlaPolicySet:
    global _cached_policy
    with _lock:
        if _cached_policy is None:
            _cached_policy = _load_from_disk()
        return _cached_policy


def reload_policy() -> SlaPolicySet:
    """Invalidate cache + re-read. Used by tests + by Compliance
    workshop hot-reload after a policy change."""
    global _cached_policy
    with _lock:
        _cached_policy = _load_from_disk()
        return _cached_policy


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def hours_for_customer_tier(tier_name: Optional[str]) -> int:
    return get_policy().hours_for_tier(tier_name)


def stamp_sla_deadline(
    *,
    opened_at: str,
    customer_tier: Optional[str],
    policy: Optional[SlaPolicySet] = None,
) -> str:
    """Compute the SLA deadline ISO string for a case open at
    ``opened_at`` for a customer of ``customer_tier``.

    Pure function — useful for tests + replay queries.
    """
    p = policy or get_policy()
    hours = p.hours_for_tier(customer_tier)
    try:
        opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        opened = datetime.now(timezone.utc)
    deadline = opened + timedelta(hours=hours)
    # Round to the second; SLA precision below seconds is meaningless.
    deadline = deadline.replace(microsecond=0)
    return deadline.isoformat()
