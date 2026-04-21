"""Local HuggingFace backend for the ASOE sandbox.

Uses Outlines + a small open-weights model to produce constrained JSON
output for intent classification, recipe selection, and shadow decisions —
exactly the same guarantee as ``OutlinesConstrainedBackend`` in production.

Configuration
-------------
LOCAL_LLM_MODEL   : HuggingFace model repo id  (default: Qwen/Qwen2.5-0.5B-Instruct)
LOCAL_LLM_DEVICE  : "cpu" | "cuda" | "mps"      (default: cpu)

Injection
---------
Set before running the sandbox UI or tests:

    export LOCAL_LLM_BACKEND_CLASS=tests.sandbox.llm.local_backend.LocalHFBackend

The constraints router picks this up via the LOCAL_LLM_BACKEND_CLASS hook.

Fallback
--------
If the model fails to load (missing transformers/outlines, no weights) the
backend gracefully delegates every call to DeterministicFallbackBackend so
the sandbox UI remains functional without GPU / large downloads.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from contracts.models import GraphState
from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.specs import IntentDecision, RecipeProposal, ShadowDecisionSchema

logger = logging.getLogger("asoe.sandbox.llm")

_DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
_DEFAULT_DEVICE = "cpu"


class LocalHFBackend:
    """Constrained-generation backend backed by a local HuggingFace model.

    Implements the same interface as ``OutlinesConstrainedBackend`` so it can
    be swapped in transparently via the LOCAL_LLM_BACKEND_CLASS env var.
    """

    def __init__(self) -> None:
        model_name = os.getenv("LOCAL_LLM_MODEL", _DEFAULT_MODEL)
        device = os.getenv("LOCAL_LLM_DEVICE", _DEFAULT_DEVICE)
        self._model = _load_model(model_name, device)
        self._fallback = DeterministicFallbackBackend()

    # ------------------------------------------------------------------
    # Public interface — matches OutlinesConstrainedBackend
    # ------------------------------------------------------------------

    def classify_intent(self, state: GraphState) -> IntentDecision:
        if self._model is None:
            logger.debug("classify_intent: model unavailable, using fallback")
            return self._fallback.classify_intent(state)
        prompt = self.intent_prompt(state)
        raw = self._model(prompt, IntentDecision, max_new_tokens=120)
        return IntentDecision.model_validate_json(raw)

    def propose_recipe(self, state: GraphState) -> Optional[RecipeProposal]:
        if self._model is None:
            logger.debug("propose_recipe: model unavailable, using fallback")
            return self._fallback.propose_recipe(state)
        prompt = self.recipe_prompt(state)
        try:
            raw = self._model(prompt, RecipeProposal, max_new_tokens=80)
            return RecipeProposal.model_validate_json(raw)
        except Exception:
            # MASS_PRICING_ERROR → no recipe; model may return invalid JSON
            return None

    def shadow_decision(self, state: GraphState) -> ShadowDecisionSchema:
        if self._model is None:
            logger.debug("shadow_decision: model unavailable, using fallback")
            return self._fallback.shadow_decision(state)
        prompt = self.shadow_prompt(state)
        raw = self._model(prompt, ShadowDecisionSchema, max_new_tokens=120)
        return ShadowDecisionSchema.model_validate_json(raw)

    # ------------------------------------------------------------------
    # Prompt builders (static — same shape as OutlinesConstrainedBackend)
    # ------------------------------------------------------------------

    @staticmethod
    def intent_prompt(state: GraphState) -> str:
        return (
            "Classify the EDI order exception into exactly one allowed intent. "
            f"order_id={state.event.order_id}; "
            f"event_type={state.event.event_type}; "
            f"po_price={state.event.po_price}; "
            f"sap_base_price={state.event.sap_base_price}; "
            f"line_count={state.event.line_count}; "
            f"requester_role={state.event.requester_role}; "
            f"credit_limit={state.event.credit_limit}; "
            f"current_exposure={state.event.current_exposure}. "
            "Allowed intents: CONTRACTUAL_CORRECTION, CREDIT_BLOCK, "
            "MASS_PRICING_ERROR, DUPLICATE_PO, PRICE_HOLD_RELEASE, "
            "EDI_MISMATCH, BACK_ORDER, OVER_MAX, MIN_ORDER_QTY, "
            "PALLET_CONFIG, DELIVERY_DELAY."
        )

    @staticmethod
    def recipe_prompt(state: GraphState) -> str:
        return (
            "Select the exact registered recipe name for the classified intent. "
            f"intent={state.intent.value}. "
            "Allowed recipes: PriceAdjustmentRecipe.py, "
            "CreditHoldReleaseRecipe.py, DuplicatePORecipe.py, "
            "PriceHoldReleaseRecipe.py, EdiMismatchRecipe.py, "
            "BackOrderResolutionRecipe.py, OverMaxTrimRecipe.py, "
            "MOQRoundUpRecipe.py, PalletAlignmentRecipe.py, "
            "DeliveryDelayResolutionRecipe.py."
        )

    @staticmethod
    def shadow_prompt(state: GraphState) -> str:
        return (
            "Return a compliance shadow verdict for this execution. "
            f"intent={state.intent.value}; "
            f"line_count={state.event.line_count}; "
            f"batch_total_variance={state.batch_total_variance}. "
            "Allowed statuses: GREEN, YELLOW, RED."
        )


# ---------------------------------------------------------------------------
# Model loader — lazy, with graceful failure
# ---------------------------------------------------------------------------


def _load_model(model_name: str, device: str):
    """Attempt to load an Outlines-wrapped HuggingFace model.

    Returns the callable model on success, or ``None`` on any error.
    The caller then falls back to DeterministicFallbackBackend.
    """
    try:
        import outlines  # noqa: PLC0415
        import outlines.models as om  # noqa: PLC0415
        import outlines.generate as og  # noqa: PLC0415

        logger.info("Loading local model: %s on %s", model_name, device)
        model = om.transformers(model_name, device=device)

        def _generate(prompt: str, schema_cls, max_new_tokens: int = 120) -> str:
            generator = og.json(model, schema_cls)
            result = generator(prompt, max_tokens=max_new_tokens)
            if isinstance(result, str):
                return result
            # outlines may return the schema instance directly
            return result.model_dump_json() if hasattr(result, "model_dump_json") else str(result)

        logger.info("Local model loaded successfully: %s", model_name)
        return _generate

    except ImportError:
        logger.warning(
            "outlines/transformers not installed. "
            "Install sandbox extras: pip install -r tests/sandbox/requirements-sandbox.txt. "
            "Falling back to DeterministicFallbackBackend."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to load local model '%s': %s. "
            "Falling back to DeterministicFallbackBackend.",
            model_name,
            exc,
        )
    return None
