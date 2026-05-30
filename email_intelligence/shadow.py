from __future__ import annotations

# ADR-036 D4 — shadow / observe harness for the live email classifier.
#
# "Until calibrated, the live classifier runs in shadow/observe and the
# deterministic shim drives behaviour." This module records the agreement
# between the live (candidate) decision and the deterministic (gating)
# decision so a promotion from shadow→gate is data-driven. It NEVER changes
# the gating decision — the caller (EmailSupergroupClassifier) decides what
# to gate on; this only observes.

import logging
import threading
from dataclasses import dataclass
from typing import List

from constraints.specs import EmailSupergroupDecision
from email_intelligence import calibration

logger = logging.getLogger("asoe.email_intelligence.shadow")


@dataclass(frozen=True)
class ShadowObservation:
    candidate_supergroup: str
    candidate_confidence: float
    gating_supergroup: str
    agreed: bool
    case_ref: str | None = None


_observations: List[ShadowObservation] = []
_lock = threading.Lock()


def observe(
    candidate: EmailSupergroupDecision,
    gating: EmailSupergroupDecision,
    *,
    case_ref: str | None = None,
) -> ShadowObservation:
    """Record a shadow comparison of the live candidate against the
    deterministic gating decision, and log the agreement. Also forwards the
    candidate to the calibration recorder (it is a live prediction whose
    outcome will be confirmed later). Returns the observation; the caller
    ignores the return for gating — shadow never gates."""
    agreed = candidate.supergroup_code == gating.supergroup_code
    obs = ShadowObservation(
        candidate_supergroup=candidate.supergroup_code,
        candidate_confidence=candidate.confidence,
        gating_supergroup=gating.supergroup_code,
        agreed=agreed,
        case_ref=case_ref,
    )
    with _lock:
        _observations.append(obs)
    calibration.record_prediction(
        candidate.supergroup_code,
        candidate.confidence,
        backend_kind="live:shadow",
        case_ref=case_ref,
    )
    logger.info(
        "email_supergroup.shadow_observation",
        extra={
            "candidate": candidate.supergroup_code,
            "candidate_confidence": round(candidate.confidence, 4),
            "gating": gating.supergroup_code,
            "agreed": agreed,
            "case_ref": case_ref,
        },
    )
    return obs


def observations() -> List[ShadowObservation]:
    """Snapshot of shadow observations (tests / a local exporter)."""
    with _lock:
        return list(_observations)


def agreement_rate() -> float | None:
    """Fraction of observations where candidate == gating. None when there
    are no observations yet (honest 'not measurable', not a fake 1.0)."""
    with _lock:
        if not _observations:
            return None
        return sum(1 for o in _observations if o.agreed) / len(_observations)


def reset() -> None:
    with _lock:
        _observations.clear()
