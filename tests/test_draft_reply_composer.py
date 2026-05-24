"""ADR-042 Phase 7 — DraftReply composer + AnalysisResponse wiring.

Projects the current AI reply draft from resolution_data["reply_draft"] (the
ReplyDraftRecipe output a DRAFT_REPLY disposition persisted in Phase 4). None
when no draft exists; flattens the nested `draft` block (Guardrail #6).
"""

from __future__ import annotations

from api.profile_composer import compose_draft_reply
from api.schemas import AnalysisResponse, DraftReply
from api.store import ExceptionRecord


def _record(resolution_data) -> ExceptionRecord:
    return ExceptionRecord(
        tenant_id="acme-corp", order_id="SO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent="MANUAL_ORDER_INTAKE",
        lifecycle_state="PENDING_REVIEW", shadow_verdict="YELLOW",
        resolution_data=resolution_data,
    )


_DRAFTED = {
    "status": "DRAFTED",
    "reason": None,
    "draft": {
        "template_name": "clarify_po",
        "recipient": "buyer@walmart.example",
        "subject": "Re: PO 0093847612",
        "body": "Hello, could you confirm the requested quantity?",
    },
    "edits_applied": [{"field": "subject", "before": "Re: PO", "after": "Re: PO 0093847612"}],
    "drafted_by": "analyst-1",
    "drafted_at": "2026-05-24T10:00:00Z",
}


def test_compose_none_when_no_draft() -> None:
    assert compose_draft_reply(_record({})) is None


def test_compose_projects_drafted_with_flattened_draft_and_edits() -> None:
    out = compose_draft_reply(_record({"reply_draft": _DRAFTED}))
    assert out is not None
    assert out.status == "DRAFTED"
    assert out.template_name == "clarify_po"
    assert out.recipient == "buyer@walmart.example"
    assert out.subject == "Re: PO 0093847612"
    assert out.body.startswith("Hello")
    assert out.edits_applied[0].field == "subject"
    assert out.drafted_by == "analyst-1"


def test_compose_projects_rejected_with_reason_and_no_body() -> None:
    rejected = {"status": "REJECTED", "reason": "No recipient on file", "draft": {}}
    out = compose_draft_reply(_record({"reply_draft": rejected}))
    assert out is not None
    assert out.status == "REJECTED"
    assert out.reason == "No recipient on file"
    assert out.body is None


def test_analysis_response_carries_draft_reply() -> None:
    ar = AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
        draft_reply=DraftReply(status="DRAFTED", subject="Hi"),
    )
    assert ar.draft_reply is not None
    assert AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
    ).draft_reply is None
