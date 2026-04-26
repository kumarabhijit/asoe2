from __future__ import annotations

# Coverage for the LLMProvenance section of
# compliance/audit_bearing_registry.yaml.
#
# Pins the panel-required audit fields (Chen review §4) and the
# pending_signoff marker. A future PR that lands compliance sign-off
# flips pending_signoff: true → false and updates this test.

from pathlib import Path

import yaml


_REGISTRY = yaml.safe_load(
    Path("compliance/audit_bearing_registry.yaml").read_text(encoding="utf-8")
)


def _llm_provenance() -> dict:
    section = _REGISTRY.get("LLMProvenance")
    assert section, (
        "compliance/audit_bearing_registry.yaml must declare an "
        "LLMProvenance section. See Chen review §4 — request_id, "
        "provider, and model_id are SOX-bearing for the V1 PR-1 "
        "remote-LLM tier."
    )
    return section


def test_llm_provenance_section_exists() -> None:
    section = _llm_provenance()
    assert {"llm_provider_used", "llm_model_id", "llm_request_id"} <= section.keys()


def test_llm_provenance_fields_are_audit_bearing() -> None:
    section = _llm_provenance()
    for field in ("llm_provider_used", "llm_model_id", "llm_request_id"):
        entry = section[field]
        assert entry["tier"] == "audit-bearing", field
        assert entry["source"] == "control", field


def test_llm_provenance_fields_pending_compliance_signoff() -> None:
    """Until the 2026-04-22 workshop's follow-up signs off on the
    remote-LLM tier, every LLMProvenance row must carry
    pending_signoff: true. CODEOWNERS gates this section so a
    compliance reviewer is requested on every PR that flips a
    pending_signoff."""
    section = _llm_provenance()
    for field in ("llm_provider_used", "llm_model_id", "llm_request_id"):
        assert section[field].get("pending_signoff") is True, field


def test_llm_provenance_fields_have_rationale() -> None:
    """Every audit-bearing classification carries a rationale that
    explains WHY it's audit-bearing (the workshop minute requirement,
    inherited from existing rows)."""
    section = _llm_provenance()
    for field in ("llm_provider_used", "llm_model_id", "llm_request_id"):
        rationale = section[field].get("rationale", "")
        assert isinstance(rationale, str)
        assert len(rationale) > 50, (
            f"{field}: rationale must explain SOX/audit motivation"
        )
