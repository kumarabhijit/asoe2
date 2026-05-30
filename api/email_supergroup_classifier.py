from __future__ import annotations

# ADR-036 — Customer-origin email supergroup classifier.
#
# Classifies an inbound CUSTOMER email DIRECTLY into one of the 12
# CUSTOMER supergroups (requirements §6.3). It is deliberately
# non-executing, mirroring IntentClassifier:
#   - calls a constrained backend to produce EmailSupergroupDecision
#   - returns supergroup_code + confidence ONLY
#
# Invariants:
#   - NO recipe is selected or called here
#   - NO Compliance Shadow is invoked here
#   - NO graph state is mutated here
#   - Output vocabulary is constrained at generation time
#     (AllowedCustomerSupergroup) — never post-hoc parsed.
#
# This differs from IntentClassifier in WHAT it emits: the supergroup is a
# case-level rollup (§8.5 — never a routing key), so the classifier emits
# the supergroup directly rather than a leaf intent. Routing continues to
# key off the leaf intent_code elsewhere.
#
# Layering: this lives in api/, NOT skills/. The routing-on-leaf invariant
# lock (tests/test_routing_on_leaf_only.py) keeps recipes/orchestration/
# skills blind to supergroup_code so no one can route on it; the component
# that *writes* the supergroup classification belongs in the api/ layer
# (alongside record_classification), exactly like the API-path classifier.
#
# Phase 1 (this module) ships the DeterministicBackend bootstrap. The live
# OutlinesConstrainedBackend (Phase 3) is gated on the email-body sanitizer
# + confidence calibration per ADR-036 §5/§6.

from typing import Optional, Protocol

from contracts.models import GraphState
from constraints.specs import EmailSupergroupDecision

# Below this confidence the decision is NOT recorded as a MODEL
# classification; the case is routed to SG_NEEDS_TRIAGE (the honest
# "couldn't classify" sink with §8.2 forcing functions). ADR-036 D4;
# env-overridable per requirements R3.
DEFAULT_CONFIDENCE_THRESHOLD = 0.85

# The category hint the upstream producer stamps on event.metadata. The
# live email-intelligence-agent will replace the hint with real
# classification; until then the deterministic backend reads it so the
# sandbox/seeder and tests are reproducible.
EMAIL_CATEGORY_HINT_KEY = "email_supergroup_hint"


class EmailSupergroupBackend(Protocol):
    def classify_email_supergroup(self, state: GraphState) -> EmailSupergroupDecision:
        ...


class DeterministicEmailSupergroupBackend:
    """Bootstrap backend (ADR-036 D5). Reads the producer's category hint
    off ``event.metadata`` and echoes it as a high-confidence decision; a
    missing/unknown hint yields a low-confidence SG_NEEDS_TRIAGE so the
    threshold sink is exercised honestly. This is a labelled stand-in for
    the live model — it invents no taxonomy, only relays the hint."""

    def __init__(self, valid_codes: Optional[frozenset[str]] = None) -> None:
        # Constrain against the schema's own enum so this can never relay a
        # code outside AllowedCustomerSupergroup.
        from typing import get_args
        from constraints.specs import AllowedCustomerSupergroup

        self._valid = valid_codes or frozenset(get_args(AllowedCustomerSupergroup))

    def classify_email_supergroup(self, state: GraphState) -> EmailSupergroupDecision:
        meta = (state.event.metadata or {}) if state.event else {}
        hint = meta.get(EMAIL_CATEGORY_HINT_KEY)
        if hint in self._valid and hint != "SG_NEEDS_TRIAGE":
            return EmailSupergroupDecision(
                supergroup_code=hint,
                confidence=0.95,
                rationale=f"deterministic backend: producer hint '{hint}'",
            )
        return EmailSupergroupDecision(
            supergroup_code="SG_NEEDS_TRIAGE",
            confidence=0.0,
            rationale="no usable supergroup hint — routed to triage",
        )


class EmailSupergroupClassifier:
    """Classify a CUSTOMER email's supergroup. Non-executing: no recipe,
    no shadow, no state mutation (ADR-036 §3)."""

    def __init__(
        self,
        backend: Optional[EmailSupergroupBackend] = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._backend = backend or DeterministicEmailSupergroupBackend()
        self._threshold = confidence_threshold

    def classify(self, state: GraphState) -> EmailSupergroupDecision:
        """Return the constrained EmailSupergroupDecision for *state*.

        Decisions at or above the confidence threshold are returned
        verbatim (caller records classifier_type=MODEL). Below-threshold
        decisions are collapsed to SG_NEEDS_TRIAGE so a low-confidence
        guess is never stamped as the case supergroup (ADR-036 D4).
        """
        decision = self._backend.classify_email_supergroup(state)
        if decision.confidence < self._threshold:
            return EmailSupergroupDecision(
                supergroup_code="SG_NEEDS_TRIAGE",
                confidence=decision.confidence,
                rationale=(
                    f"confidence {decision.confidence:.2f} < "
                    f"{self._threshold:.2f} → triage "
                    f"(would have been {decision.supergroup_code})"
                ),
            )
        return decision
