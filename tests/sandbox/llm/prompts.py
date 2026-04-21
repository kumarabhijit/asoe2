"""Prompt templates for the ASOE sandbox local LLM backend.

These are standalone helper functions that build prompts from an EDI event
dict (as returned by ``seed.load_events()``).  They are used by the
Streamlit UI to display what the model receives before constrained generation.

The LocalHFBackend uses GraphState-aware static methods on the class itself
for actual generation.  These templates are the human-readable equivalents
for the UI "Prompt Preview" expander.
"""
from __future__ import annotations

from typing import Any, Dict


def intent_prompt(event: Dict[str, Any]) -> str:
    """Build the intent-classification prompt from a raw EDI event dict."""
    import json

    metadata: Dict[str, Any] = {}
    raw_meta = event.get("metadata", "{}")
    if isinstance(raw_meta, str):
        try:
            metadata = json.loads(raw_meta)
        except Exception:  # noqa: BLE001
            pass
    else:
        metadata = raw_meta

    return (
        "You are a compliance-aware order exception classifier.\n"
        "Classify the following EDI order exception into exactly one allowed intent.\n\n"
        f"order_id       : {event.get('order_id', 'N/A')}\n"
        f"event_type     : {event.get('event_type', 'N/A')}\n"
        f"retailer_id    : {event.get('retailer_id', 'N/A')}\n"
        f"sku            : {event.get('sku', 'N/A')}\n"
        f"po_price       : {event.get('po_price', 'N/A')}\n"
        f"sap_price      : {event.get('sap_price', 'N/A')}\n"
        f"line_count     : {event.get('line_count', 1)}\n"
        f"metadata       : {json.dumps(metadata, indent=2)}\n\n"
        "Allowed intents:\n"
        "  - CONTRACTUAL_CORRECTION\n"
        "  - CREDIT_BLOCK\n"
        "  - MASS_PRICING_ERROR\n"
        "  - DUPLICATE_PO\n"
        "  - PRICE_HOLD_RELEASE\n"
        "  - EDI_MISMATCH\n\n"
        "Return JSON conforming to IntentDecision schema: "
        '{"intent": "<value>", "confidence": <0.0-1.0>, "rationale": "<string>"}'
    )


def recipe_prompt(intent: str) -> str:
    """Build the recipe-selection prompt given a classified intent string."""
    return (
        "You are a deterministic recipe selector for an order-to-cash exception system.\n"
        f"The classified intent is: {intent}\n\n"
        "Select the exact registered recipe name from the allowed list:\n"
        "  - PriceAdjustmentRecipe.py       (for CONTRACTUAL_CORRECTION)\n"
        "  - CreditHoldReleaseRecipe.py     (for CREDIT_BLOCK)\n"
        "  - DuplicatePORecipe.py           (for DUPLICATE_PO)\n"
        "  - PriceHoldReleaseRecipe.py      (for PRICE_HOLD_RELEASE)\n"
        "  - EdiMismatchRecipe.py           (for EDI_MISMATCH)\n"
        "  NOTE: MASS_PRICING_ERROR has no recipe — return null.\n\n"
        "Return JSON conforming to RecipeProposal schema: "
        '{"recipe_name": "<value>"}'
    )


def shadow_prompt(intent: str, line_count: int, batch_variance: float) -> str:
    """Build the compliance shadow prompt."""
    return (
        "You are the Compliance Shadow for an order-to-cash automation system.\n"
        "Evaluate the proposed execution and return a verdict.\n\n"
        f"intent              : {intent}\n"
        f"line_count          : {line_count}\n"
        f"batch_total_variance: {batch_variance}\n\n"
        "Policy rules:\n"
        "  - MASS_PRICING_ERROR                    → always RED\n"
        "  - line_count > 10                       → RED (mass update risk)\n"
        "  - batch_total_variance > 10000          → RED (circuit breaker)\n"
        "  - CREDIT_BLOCK                          → YELLOW (manual review)\n"
        "  - PRICE_HOLD_RELEASE                    → GREEN/YELLOW/RED by |variance| vs tolerance\n"
        "  - EDI_MISMATCH (SKU_MISMATCH)           → RED (hard reject)\n"
        "  - EDI_MISMATCH (QTY/UOM/SHIP_TO)        → YELLOW (manual review)\n"
        "  - otherwise                             → GREEN\n\n"
        "Return JSON conforming to ShadowDecisionSchema: "
        '{"status": "<GREEN|YELLOW|RED>", "reasons": ["..."], "policy_hits": ["..."]}'
    )
