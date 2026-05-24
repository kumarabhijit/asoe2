from __future__ import annotations

# Reply-Draft Recipe (ADR-042 Phase 4 — the AI Draft Reply compose leg).
#
# Deterministic composition of a buyer-facing reply (subject + body) from a
# fixed, reviewable template and the operator-reviewed case context. This is the
# "generate" + "edit" of the prototype's gen/edit/approve/send flow; the actual
# send is a separate, operator-authorised SEND_REPLY disposition that fires the
# buyer_notification gateway as a declared GatewayEffect.
#
# Guardrail #1 (skills guide; recipes execute): all composition lives here as a
# pure function. No I/O, no LLM call, no gateway call — exactly like
# SubmitToErpRecipe builds the ERP payload. The email body is human-facing text
# the operator reviews before sending, so a deterministic template render (not a
# constrained-generation contract) is the correct home for the MLS; an
# LLM-authored body would be a future constrained/recorded `email_draft` gateway
# (mirroring the extraction gateway), not inline logic here.
#
# Inputs:
#   order_id      : str               — host trace / order identifier
#   recipient     : Optional[str]     — resolved buyer email (REJECTED if absent)
#   customer_name : Optional[str]     — greeting name
#   template_name : str               — one of _TEMPLATES (REJECTED if unknown;
#                                        never fabricate a template)
#   context       : Dict[str, Any]    — fields the template interpolates
#                                        (e.g. customer_po, clarification_points)
#   edits         : Optional[Dict]    — operator overrides {"subject"/"body": str}
#
# Output (Dict[str, Any]):
#   status        : "DRAFTED" | "REJECTED"
#   reason        : Optional[str]      — populated when REJECTED
#   draft         : {template_name, recipient, subject, body} | {} when REJECTED
#   edits_applied : List[Dict]         — before/after audit of operator overrides
#
# Invariants:
#   - Pure function: no side effects; same inputs → identical output.
#   - REJECTED (a valid terminal — Guardrail #5) with no recipient or an unknown
#     template; the draft is empty in that case.
#   - Unknown context placeholders render as empty strings (never the literal
#     "{field}") — deterministic, no KeyError, no fabricated content.

from typing import Any, Dict, List, Optional

# Canonical, reviewable templates keyed by the `notification_template` names the
# classifier recipes already emit (ManualOrderIntakeRecipe / EdiMismatchRecipe /
# DuplicatePORecipe). The MLS ships the order-entry clarification template; more
# templates are added here as their flows are wired.
_TEMPLATES: Dict[str, Dict[str, str]] = {
    "email_order_clarification_request": {
        "subject": "Re: Your purchase order {customer_po} — a few details to confirm",
        "body": (
            "Hello {customer_name},\n\n"
            "Thank you for your order. Before we can enter it, could you please "
            "confirm the following:\n\n"
            "{clarification_block}\n\n"
            "Once you confirm, we'll process the order right away.\n\n"
            "Best regards,\n"
            "Order Management Team"
        ),
    },
}

# Fields the operator may override on the rendered draft.
_EDITABLE_FIELDS: tuple[str, ...] = ("subject", "body")


class _SafeDict(dict):
    """str.format_map mapping that renders absent placeholders as "" — keeps the
    render deterministic and KeyError-free without fabricating content."""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return ""


def compose_reply_draft(
    *,
    order_id: str,
    recipient: Optional[str],
    template_name: str,
    customer_name: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    edits: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compose a buyer reply draft. See the module docstring for the contract."""
    context = context or {}
    edits = edits or {}

    if not recipient or not str(recipient).strip():
        return _rejected("No buyer recipient to send the draft to.")

    template = _TEMPLATES.get(template_name)
    if template is None:
        return _rejected(f"Unknown reply template {template_name!r}.")

    fmt = _SafeDict(context)
    fmt["customer_name"] = customer_name or "there"
    fmt["clarification_block"] = _bullets(context.get("clarification_points"))

    rendered: Dict[str, str] = {
        "subject": template["subject"].format_map(fmt),
        "body": template["body"].format_map(fmt),
    }

    edits_applied: List[Dict[str, Any]] = []
    for field in _EDITABLE_FIELDS:
        after = edits.get(field)
        if after is None:
            continue
        before = rendered[field]
        if before != after:
            rendered[field] = after
            edits_applied.append({"field": field, "before": before, "after": after})

    return {
        "status": "DRAFTED",
        "reason": None,
        "draft": {
            "template_name": template_name,
            "recipient": recipient,
            "subject": rendered["subject"],
            "body": rendered["body"],
        },
        "edits_applied": edits_applied,
    }


def _bullets(points: Any) -> str:
    if not points:
        return ""
    return "\n".join(f"- {p}" for p in points)


def _rejected(reason: str) -> Dict[str, Any]:
    return {"status": "REJECTED", "reason": reason, "draft": {}, "edits_applied": []}
