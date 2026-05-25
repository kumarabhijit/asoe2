"""CP-B RED gate (ADR-043 §3) — EvidenceAnchor is registered audit-bearing,
but on-screen position is NOT.

Test-first (`xfail(strict=True)`; removed at CP-C with the registry rows +
CODEOWNERS compliance review). `EvidenceAnchor` does not end in `AnalysisData`,
so the generic coverage fitness test does not auto-enforce it — this dedicated
gate does. It also encodes D2: the evidence tuple is audit-bearing; `page`/`bbox`
(best-effort position) must never be classified audit-bearing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.xfail(
    reason="ADR-043 EvidenceAnchor registry section lands at CP-C",
    strict=True,
)

_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "compliance"
    / "audit_bearing_registry.yaml"
)


def _registry() -> dict:
    return yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8"))


def test_evidence_anchor_section_exists():
    assert "EvidenceAnchor" in _registry()


def test_evidence_tuple_fields_are_audit_bearing():
    section = _registry()["EvidenceAnchor"]
    for field in ("attachment_id", "source_sha256", "text", "supports_ref"):
        assert section.get(field, {}).get("tier") == "audit-bearing", field


def test_on_screen_position_is_not_audit_bearing():
    # The box may be wrong/approximate; it can never be the audit unit.
    section = _registry()["EvidenceAnchor"]
    for field in ("page", "bbox"):
        entry = section.get(field)
        if entry is not None:
            assert entry.get("tier") != "audit-bearing", field
