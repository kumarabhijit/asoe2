"""ADR-042 Phase 6 — ChangeAnalysis composer + AnalysisResponse wiring.

Composer projects the deterministic evaluation from enrichment_context; None
until the change_analysis producer lands (preview-only, Guardrail #6). Evaluation
logic is locked in tests/test_change_analysis_recipe.py; here we lock the
projection + None-on-absent/malformed + the AnalysisResponse field.
"""

from __future__ import annotations

from api.profile_composer import compose_change_analysis
from api.schemas import AnalysisResponse, ChangeAnalysis
from api.store import ExceptionRecord
from contracts.policy import HIGH_VALUE_OVERRIDE_THRESHOLD_USD
from recipes.ChangeAnalysisRecipe import evaluate_change


def _record(**overrides) -> ExceptionRecord:
    base = dict(
        tenant_id="acme-corp", order_id="SO-1", event_type="MANUAL_ORDER_INTAKE",
        trace_id="tr-1", intent="MANUAL_ORDER_INTAKE",
        lifecycle_state="PENDING_REVIEW", shadow_verdict="YELLOW",
        resolution_data={},
    )
    base.update(overrides)
    return ExceptionRecord(**base)


_CTX = evaluate_change(
    order_id="SO-1", order_value_usd=45200.0,
    cosign_threshold_usd=HIGH_VALUE_OVERRIDE_THRESHOLD_USD, lifecycle_index=2,
    change_items=[{"field": "quantity", "from_value": "480", "to_value": "600"}],
    signals={"inventory": {"atp": 520, "required": 600},
             "sla": {"within_window": True, "days_to_deadline": 1}},
)


def test_compose_none_when_context_absent() -> None:
    assert compose_change_analysis(_record()) is None


def test_compose_projects_from_context() -> None:
    out = compose_change_analysis(
        _record(enrichment_context={"change_analysis": _CTX})
    )
    assert out is not None
    assert out.decision.requires_cosign is True  # 45200 >= threshold
    assert any(c.name == "Financial" for c in out.evaluation.checks)
    assert out.evaluation.change_items[0].to_value == "600"
    assert len(out.scenarios) >= 1


def test_compose_none_on_malformed() -> None:
    bad = {"scenarios": []}  # missing required evaluation + decision
    assert compose_change_analysis(
        _record(enrichment_context={"change_analysis": bad})
    ) is None


def test_analysis_response_carries_change_analysis() -> None:
    ar = AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
        change_analysis=ChangeAnalysis(**_CTX),
    )
    assert ar.change_analysis is not None
    assert AnalysisResponse(
        diagnosis="d", confidence=90, risk="low", resolution="r",
    ).change_analysis is None
