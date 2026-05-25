"""CP-B RED gate (ADR-043 §2.2) — the `EvidenceAnchor` contract.

Test-first per `docs/test-strategy/customer-inbox-tdd-strategy.md` §3: landed
before its production code as `xfail(strict=True)` so CI stays green-and-honest
("spec pending"). The marker is removed at CP-C when the schema lands — at which
point a strict XPASS forces the deliberate removal.

Pins the converged decisions D2/D4:
  * one schema spanning both phases via an `anchor_source` discriminator;
  * Phase-1 (`text_derived`) anchors MUST carry no geometry (page/bbox/conf);
  * `supports_kind` is a CLOSED vocabulary;
  * `source_sha256` binds the anchor to exact bytes;
  * the audit-authoritative unit excludes on-screen position.
"""

from __future__ import annotations

import pytest


def _text_anchor(**overrides):
    from api.schemas import EvidenceAnchor, MatchKey

    base = dict(
        attachment_id="att-1",
        anchor_source="text_derived",
        text="PO-2026-0042",
        match_key=MatchKey(normalized_text="po-2026-0042", occurrence_index=0),
        supports_kind="extracted_field",
        supports_ref="order_entry.po_number",
        label="PO number",
        source_sha256="a" * 64,
    )
    base.update(overrides)
    return EvidenceAnchor(**base)


def test_text_anchor_constructs_with_no_geometry():
    a = _text_anchor()
    assert a.anchor_source == "text_derived"
    assert a.page is None and a.bbox is None and a.confidence is None
    assert a.source_sha256 == "a" * 64


def test_text_derived_anchor_rejects_geometry():
    # The Phase-1 safety invariant: a text-derived anchor cannot smuggle in a
    # spatial box (that is ADR-045 territory, gated on a verifier).
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _text_anchor(page=1, bbox=[0.1, 0.1, 0.2, 0.2])


def test_supports_kind_is_a_closed_vocabulary():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _text_anchor(supports_kind="something_invented")


def test_source_sha256_is_required():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _text_anchor(source_sha256=None)


def test_audit_tuple_excludes_on_screen_position():
    # D2: the audit-authoritative unit is (attachment_id, sha256, text,
    # supports_ref) — never page/bbox. A spatial anchor and a text anchor with
    # the same evidence share an identity; only the rendered position differs.
    a = _text_anchor()
    tup = a.audit_tuple()
    assert tup == ("att-1", "a" * 64, "PO-2026-0042", "order_entry.po_number")
    assert 1 not in tup  # no page
