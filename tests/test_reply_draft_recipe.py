"""ADR-042 Phase 4 — ReplyDraftRecipe (deterministic buyer-reply composition).

The compose leg of AI Draft Reply (gen/edit/approve/send). A pure function that
renders a fixed, reviewable email template into a {subject, body} draft from the
operator-reviewed case context, applies operator edits with a before/after audit,
and validates that there is a buyer to send to. No I/O, no LLM call — composition
is deterministic business logic (Guardrail #1), exactly like SubmitToErpRecipe
builds the ERP payload. The graph applies the actual send as a declared
GatewayEffect on a later operator-authorised SEND_REPLY (a separate increment).

Written test-first.
"""

from __future__ import annotations

import pytest

from recipes.ReplyDraftRecipe import compose_reply_draft

_CTX = {
    "customer_po": "0093847612",
    "clarification_points": [
        "Requested delivery date for line 001 (BEV-COLA-12PK).",
        "Confirm the unit of measure — cases (CS) or eaches (EA)?",
    ],
}


def _compose(**over):
    kwargs = dict(
        order_id="EML-PO-1",
        recipient="orders@walmart.example",
        customer_name="Walmart Stores Inc",
        template_name="email_order_clarification_request",
        context=_CTX,
    )
    kwargs.update(over)
    return compose_reply_draft(**kwargs)


def test_composes_subject_and_body_from_template() -> None:
    out = _compose()
    assert out["status"] == "DRAFTED"
    assert out["reason"] is None
    draft = out["draft"]
    assert draft["recipient"] == "orders@walmart.example"
    assert draft["template_name"] == "email_order_clarification_request"
    # Context interpolated into subject + body.
    assert "0093847612" in draft["subject"]
    assert "Walmart Stores Inc" in draft["body"]
    # Clarification points render as a bullet list.
    assert "- Requested delivery date for line 001 (BEV-COLA-12PK)." in draft["body"]
    assert "- Confirm the unit of measure" in draft["body"]
    assert out["edits_applied"] == []


def test_no_recipient_is_rejected() -> None:
    out = _compose(recipient=None)
    assert out["status"] == "REJECTED"
    assert "recipient" in out["reason"].lower()
    assert out["draft"] == {}


def test_unknown_template_is_rejected_not_fabricated() -> None:
    out = _compose(template_name="totally_made_up_template")
    assert out["status"] == "REJECTED"
    assert "template" in out["reason"].lower()
    assert out["draft"] == {}


def test_operator_edits_override_with_before_after_audit() -> None:
    base = _compose()
    edited = _compose(edits={"subject": "Quick question about your PO",
                             "body": "Hi — can you confirm the delivery date? Thanks."})
    d = edited["draft"]
    assert d["subject"] == "Quick question about your PO"
    assert d["body"] == "Hi — can you confirm the delivery date? Thanks."
    audit = {a["field"]: a for a in edited["edits_applied"]}
    assert audit["subject"]["before"] == base["draft"]["subject"]
    assert audit["subject"]["after"] == "Quick question about your PO"
    assert audit["body"]["before"] == base["draft"]["body"]


def test_noop_edit_is_not_audited() -> None:
    base = _compose()
    # Re-supplying the identical rendered subject is not a change.
    out = _compose(edits={"subject": base["draft"]["subject"]})
    assert out["edits_applied"] == []


def test_missing_context_placeholder_does_not_crash() -> None:
    out = _compose(context={"clarification_points": ["Confirm quantity."]})
    assert out["status"] == "DRAFTED"
    # Absent customer_po renders as empty, never the literal "{customer_po}".
    assert "{customer_po}" not in out["draft"]["subject"]
    assert "{" not in out["draft"]["body"]


def test_is_deterministic() -> None:
    assert _compose() == _compose()
