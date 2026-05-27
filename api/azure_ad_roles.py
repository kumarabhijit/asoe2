"""Entra ID group id → ASOE role mapping — PARITY-3b.

The PARITY-3a frontend contract is that ``user.roles`` is a flat list
of strings (``analyst`` / ``manager`` / ``admin`` / ``viewer`` /
``partner``). When the backend authenticates an Entra token, it looks
at the token's ``groups`` claim and projects it into that contract via
this module.

Configuration env var ``ASOE_AZURE_AD_GROUP_TO_ROLE`` is a
comma-separated list of ``group-id:role`` pairs, e.g.::

    ASOE_AZURE_AD_GROUP_TO_ROLE=
      9f2c…aaa:analyst,
      9f2c…mmm:manager,
      9f2c…zzz:admin

Unmapped groups are dropped silently. The resulting empty role list
flows up to the middleware's no-role-for-tenant branch (PARITY-3a
``/403``).

The reverse contract — *which* tenant a user is operating against —
comes from the Entra ``tid`` claim. The validator
(``api.deps._entra_decode``) pins ``tid`` against the configured
``ASOE_ISSUER_URL`` and is the gate that contains single-tenant blast
radius today (Decision Q1).
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional

logger = logging.getLogger("asoe.api.azure_ad_roles")


def _parse_mapping(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        gid, role = chunk.split(":", 1)
        gid = gid.strip()
        role = role.strip()
        if gid and role:
            out[gid] = role
    return out


def _current_mapping() -> dict[str, str]:
    """Read the mapping fresh on every call.

    Container Apps re-imports modules on revision restart; reading the
    env var fresh keeps a hot-reload of the env (during incident
    response) from requiring a code redeploy. The mapping is tiny — a
    handful of group ids per tenant — so the per-call parse is cheap.
    """
    raw = os.getenv("ASOE_AZURE_AD_GROUP_TO_ROLE", "")
    return _parse_mapping(raw)


def groups_to_roles(groups: Optional[Iterable[str]]) -> List[str]:
    """Project Entra ``groups`` claim → ASOE role list.

    Returns a deduped, ordered list. Unmapped groups are dropped.
    """
    if not groups:
        return []
    mapping = _current_mapping()
    seen: set[str] = set()
    out: List[str] = []
    for gid in groups:
        if not gid:
            continue
        role = mapping.get(gid)
        if role and role not in seen:
            seen.add(role)
            out.append(role)
    return out
