"""Refresh-token revocation store — PARITY-3b.

When a user signs out, changes their password, or loses access via a
group-membership change, their outstanding refresh tokens must be
invalidated even though the token itself hasn't reached its exp. This
module is the revocation index the ``POST /api/auth/refresh`` route
consults before issuing new pairs.

Storage shape is intentionally tiny — we record a ``jti``
(refresh-token id) per revocation entry, with a ``reason`` for audit.
The token itself never lands here; only its id.

Backend:

* **In-process default** — a process-local set, suitable for single-
  replica dev/test runs and the seed-mode preprod scaffold.
* **Postgres-backed (TODO)** — a future migration adds a
  ``refresh_token_revocations`` table so revocations survive a
  Container App revision restart. The interface here is the seam.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger("asoe.api.refresh_token_revocation")


@dataclass(frozen=True)
class RevocationEntry:
    jti: str
    revoked_at: int
    reason: str


_STORE: Dict[str, RevocationEntry] = {}
_LOCK = threading.RLock()


def reset_store() -> None:
    """Drop all in-memory revocations. Test helper; safe in prod too."""
    with _LOCK:
        _STORE.clear()


def revoke(jti: str, reason: str = "operator-action") -> RevocationEntry:
    """Mark a refresh-token jti as revoked.

    Idempotent: a second call returns the original entry rather than
    overwriting (so the audit trail records the first revocation
    cause, not the duplicate).
    """
    if not jti:
        raise ValueError("revoke() requires a non-empty jti")
    with _LOCK:
        existing = _STORE.get(jti)
        if existing is not None:
            logger.info("revocation duplicate ignored jti=%s", jti)
            return existing
        entry = RevocationEntry(jti=jti, revoked_at=int(time.time()), reason=reason)
        _STORE[jti] = entry
        logger.info("refresh token revoked jti=%s reason=%s", jti, reason)
        return entry


def is_revoked(jti: str) -> bool:
    """True iff the jti has been revoked."""
    if not jti:
        return False
    with _LOCK:
        return jti in _STORE


def get_entry(jti: str) -> RevocationEntry | None:
    with _LOCK:
        return _STORE.get(jti)
