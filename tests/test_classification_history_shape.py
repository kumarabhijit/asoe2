"""Contract test — ClassificationHistoryEntry mirrors ClassificationEvent.

``ClassificationHistoryEntry`` (api/schemas.py) is the response-layer
projection of ``ClassificationEvent`` (contracts/models.py). The two
must carry an identical field set: a model_dump → ** spread on the
construction site would silently drop any field present only on the
source, and a UI consumer would lose audit-bearing data without an
error.

Lock the parity here so a future contract change has to be deliberate.
"""

from __future__ import annotations

from api.schemas import ClassificationHistoryEntry
from contracts.models import ClassificationEvent


def test_field_sets_match():
    event_fields = set(ClassificationEvent.model_fields.keys())
    entry_fields = set(ClassificationHistoryEntry.model_fields.keys())
    assert event_fields == entry_fields, (
        f"ClassificationEvent fields = {sorted(event_fields)}\n"
        f"ClassificationHistoryEntry fields = {sorted(entry_fields)}\n"
        f"Difference: {event_fields ^ entry_fields}"
    )


def test_classifier_type_is_a_constrained_literal_not_str():
    """The response type must surface ``ClassifierType`` (Literal), not
    plain ``str``, so OpenAPI generators emit a 3-value enum and
    front-end / partner SDK clients get compile-time enforcement."""
    import typing
    annotation = ClassificationHistoryEntry.model_fields["classifier_type"].annotation
    args = typing.get_args(annotation)
    assert set(args) == {"HUMAN", "MODEL", "RULE"}, (
        f"classifier_type annotation = {annotation!r} — expected "
        f"Literal['HUMAN','MODEL','RULE']"
    )


def test_extra_fields_forbidden_on_response_entry():
    """A future commit that adds a field to ClassificationEvent must
    also add it to ClassificationHistoryEntry. extra='forbid' ensures
    the construction site (``ClassificationHistoryEntry(**event.model_dump())``)
    raises rather than silently dropping the field."""
    import pytest
    with pytest.raises(Exception):  # ValidationError
        ClassificationHistoryEntry(  # type: ignore[call-arg]
            id="x", case_id="x", supergroup_code="SG_NEW_ORDER",
            classified_at="x", classified_by="x", classifier_type="HUMAN",
            taxonomy_version="x", new_field_not_on_event="oops",
        )


def test_redact_for_partner_blanks_reason_text():
    """The partner redaction blanks operator-authored free text but
    preserves the structural audit (who, when, what super-group)."""
    entry = ClassificationHistoryEntry(
        id="x", case_id="x", supergroup_code="SG_NEW_ORDER",
        intent_code="INT_MANUAL_ORDER_INTAKE",
        classified_at="2026-05-27T00:00:00Z",
        classified_by="user:csr-1", classifier_type="HUMAN",
        reason_text="Customer escalation risk — internal flag",
        taxonomy_version="2026-05-27-v1",
    )
    redacted = entry.redact_for_partner()
    assert redacted.reason_text is None
    # Everything else preserved.
    assert redacted.id == entry.id
    assert redacted.case_id == entry.case_id
    assert redacted.classified_by == entry.classified_by
    assert redacted.classifier_type == entry.classifier_type
    assert redacted.supergroup_code == entry.supergroup_code
    assert redacted.intent_code == entry.intent_code
    assert redacted.taxonomy_version == entry.taxonomy_version
