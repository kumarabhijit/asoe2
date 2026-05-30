"""email_intelligence — customer-email classification engine (ADR-036).

The dedicated non-routing module foreseen in ADR-036 §3.1: it reasons
(LLM / rules / — and, when wired, OCR-derived attachment text) but its
output is the CUSTOMER **supergroup**, a reporting rollup that never
reaches recipe dispatch (§8.5). It therefore lives outside the routing
layer (recipes/ orchestration/ skills/), so the routing-on-leaf invariant
lock keeps holding.

Public surface:
    EmailSupergroupClassifier — orchestrates a constrained backend
        (deterministic shim ⇄ live LLM, selected by constraints.router),
        applies the confidence threshold, and runs the live model in
        shadow/observe alongside the deterministic gate until calibrated.
    EmailSupergroupDecision — the constrained schema (re-exported from
        constraints.specs).
"""

from __future__ import annotations

from constraints.specs import EmailSupergroupDecision
from constraints.fallback_backend import EMAIL_SUPERGROUP_HINT_KEY
from email_intelligence.classifier import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    EmailSupergroupClassifier,
)

# Back-compat alias: Phase 1 named the metadata key EMAIL_CATEGORY_HINT_KEY.
EMAIL_CATEGORY_HINT_KEY = EMAIL_SUPERGROUP_HINT_KEY

__all__ = (
    "EmailSupergroupClassifier",
    "EmailSupergroupDecision",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "EMAIL_CATEGORY_HINT_KEY",
    "EMAIL_SUPERGROUP_HINT_KEY",
)
