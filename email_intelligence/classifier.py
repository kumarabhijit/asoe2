from __future__ import annotations

# ADR-036 — Customer-origin email supergroup classifier (Phases 1 + 3).
#
# Classifies an inbound CUSTOMER email DIRECTLY into one of the CUSTOMER
# supergroups (D1). Deliberately non-executing: no recipe, no Compliance
# Shadow, no graph-state mutation. Output vocabulary is constrained at
# generation time (EmailSupergroupDecision / AllowedCustomerSupergroup).
#
# Backend selection is delegated to constraints.router.get_constrained_backend
# ("email_supergroup"), which gives us, for free:
#   - graceful fallback to the deterministic shim when no model is
#     configured (local / CI / Vercel preview, or ASOE_LLM_PROVIDER unset),
#   - a live RemoteLLMBackend when an operator sets
#     ASOE_LLM_PROVIDER[_EMAIL_SUPERGROUP], with the email text fenced by
#     the sanitizer before it reaches the model,
#   - kill-switch / explain-mode / per-task disable, all honoured.
#
# Until the model's confidence is calibrated (D4), the live decision runs
# in SHADOW: it is observed + recorded for ECE, but the deterministic shim
# drives the gating decision. Flip ASOE_EMAIL_SUPERGROUP_SHADOW=0 to promote
# the live model to the gate once calibration lands.

import os
from typing import Optional

from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.specs import EmailSupergroupDecision
from contracts.models import GraphState
from email_intelligence import calibration, shadow

# Below this confidence the decision is NOT recorded as a confident
# classification; the case is routed to SG_NEEDS_TRIAGE (the honest sink
# with §8.2 forcing functions). ADR-036 D4; env-overridable per R3.
DEFAULT_CONFIDENCE_THRESHOLD = 0.85


def _resolve_threshold(explicit: Optional[float]) -> float:
    if explicit is not None:
        return explicit
    raw = os.environ.get("ASOE_EMAIL_SUPERGROUP_THRESHOLD", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_CONFIDENCE_THRESHOLD


def _shadow_active() -> bool:
    # Shadow is ON by default (pre-calibration). Operators promote the live
    # model to the gate by setting ASOE_EMAIL_SUPERGROUP_SHADOW=0.
    return os.environ.get("ASOE_EMAIL_SUPERGROUP_SHADOW", "1").strip() != "0"


def _is_deterministic(backend: object) -> bool:
    return isinstance(backend, DeterministicFallbackBackend)


class EmailSupergroupClassifier:
    """Classify a CUSTOMER email's supergroup. Non-executing (ADR-036 §3)."""

    def __init__(
        self,
        backend: object | None = None,
        confidence_threshold: Optional[float] = None,
    ) -> None:
        # If no backend is injected, resolve per-call via the router so
        # runtime env changes (provider, kill-switch) take effect without a
        # restart — matching the trio's contract.
        self._injected_backend = backend
        self._threshold = _resolve_threshold(confidence_threshold)

    def _resolve_backend(self) -> object:
        if self._injected_backend is not None:
            return self._injected_backend
        from constraints.router import get_constrained_backend

        return get_constrained_backend("email_supergroup")

    @staticmethod
    def _safe_call(backend: object, state: GraphState) -> EmailSupergroupDecision:
        """Call the backend's classifier, falling back to the deterministic
        hint-relay on any error or a backend that doesn't implement the
        method. The email path must never block case intake."""
        try:
            method = getattr(backend, "classify_email_supergroup", None)
            if method is None:
                return DeterministicFallbackBackend().classify_email_supergroup(state)
            return method(state)
        except Exception:  # pragma: no cover - defensive; never block intake
            return DeterministicFallbackBackend().classify_email_supergroup(state)

    def classify(self, state: GraphState) -> EmailSupergroupDecision:
        """Return the constrained, threshold-applied EmailSupergroupDecision.

        Gating policy:
          - deterministic backend configured (default) → it gates directly.
          - live backend configured + shadow active (default) → the live
            decision is observed/recorded but the deterministic shim gates.
          - live backend configured + shadow off (post-calibration) → the
            live decision gates.
        """
        backend = self._resolve_backend()
        primary = self._safe_call(backend, state)

        # Shadow/observe applies only to a ROUTER-RESOLVED live backend. An
        # explicitly injected backend is the caller's deliberate choice and
        # gates directly (tests, or a caller pinning a backend).
        router_live = self._injected_backend is None and not _is_deterministic(backend)

        if router_live and _shadow_active():
            # Live model runs in shadow — observe, don't gate (D4).
            gating = DeterministicFallbackBackend().classify_email_supergroup(state)
            case_ref = getattr(state.event, "order_id", None)
            shadow.observe(primary, gating, case_ref=case_ref)
        else:
            gating = primary
            if router_live:
                # Live model is the gate (promoted) — record for ongoing ECE.
                calibration.record_prediction(
                    gating.supergroup_code,
                    gating.confidence,
                    backend_kind="live:gating",
                    case_ref=getattr(state.event, "order_id", None),
                )

        return self._apply_threshold(gating)

    def _apply_threshold(
        self, decision: EmailSupergroupDecision
    ) -> EmailSupergroupDecision:
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
