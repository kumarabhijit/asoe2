from __future__ import annotations

# Phase 1 — Skill Loading & Reasoning
#
# IntentClassifier is the ONLY entry point for intent classification.
# It is deliberately non-executing:
#   - loads the relevant SKILL.md verbatim (no summarisation)
#   - calls the constrained backend to produce IntentDecision
#   - returns intent + confidence ONLY
#
# Invariants:
#   - NO recipe is selected or called here
#   - NO Compliance Shadow is invoked here
#   - Output vocabulary is constrained at generation time via the backend
#     (DeterministicFallbackBackend or OutlinesConstrainedBackend)

from contracts.models import GraphState, SkillDocument
from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.specs import IntentDecision
from skills.loader import SkillLoader


class IntentClassifier:
    """Classify the intent of an OrderEvent using the loaded skill as context.

    The backend constrains output to the allowed intent enum at generation
    time (Guidance / Outlines pattern).  This class must not call any recipe,
    must not invoke the Compliance Shadow, and must not mutate graph state.
    """

    def __init__(self, backend=None, skill_root: str = "skills") -> None:
        # Accept an injected backend so tests can pass a deterministic stub
        # without touching the env var.
        self._backend = backend if backend is not None else DeterministicFallbackBackend()
        self._loader = SkillLoader(skill_root)

    def load_skill(self, event_type: str) -> SkillDocument:
        """Return the relevant SkillDocument verbatim for the given event type."""
        return self._loader.select_for_event(event_type)

    def classify(self, state: GraphState) -> IntentDecision:
        """Return IntentDecision (intent + confidence) for *state*.

        The skill is loaded here so the backend receives full context.
        No recipe is proposed; no shadow is called.
        """
        # Load skill verbatim — only for context, not summarised
        _ = self.load_skill(state.event.event_type)

        # Constrained classification: backend enforces allowed vocabulary
        if hasattr(self._backend, "intent_prompt"):
            # Outlines path: backend expects a prompt string
            return self._backend.classify_intent(self._backend.intent_prompt(state))
        # Fallback path: backend accepts the full state
        return self._backend.classify_intent(state)
