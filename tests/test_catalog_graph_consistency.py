"""Catalog ⇄ graph disposition consistency lock.

Every dispositioned EDI ``scenarios:`` row in
``fixtures/scenarios/catalog.yaml`` carries a declared ``intent`` and
``lifecycle`` (and ``shadow_verdict``). Those declarations are what the
asoe-ui mock queue projects (``CATALOG_EXCEPTIONS`` via
``scripts/gen-mock-data.ts``) AND what the sandbox bootstrap
(``api/sandbox_bootstrap.py``) persists by running the row through the
real Skill-Shadow-Recipe graph.

If the declared disposition drifts from what the graph actually
produces, the Vercel mock and the real backend show different states for
the same scenario — the exact parity bug this fixture exists to kill.

This lock runs every dispositioned row through the **same** event
projector (``_scenario_to_order_event``) and state resolver
(``_resolve_state``) the bootstrap uses, with the sandbox gateways
registered exactly as ``api/app.py`` does at startup, and asserts:

  * the graph-classified ``intent`` matches the declared ``intent``;
  * the terminal ``lifecycle`` (via ``STATUS_TO_LIFECYCLE``) matches the
    declared ``lifecycle``;
  * the Compliance Shadow ``verdict`` matches the declared
    ``shadow_verdict`` when one is declared.

No LLM is involved — ``run_graph`` classifies via the deterministic
fallback backend in the sandbox/CI default, so this is reproducible.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from api.sandbox_bootstrap import _scenario_to_order_event
from api.sandbox_gateways import register_sandbox_gateways
from contracts.models import STATUS_TO_LIFECYCLE, GraphState

_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "catalog.yaml"
)
_TENANT = "acme-corp"


def _val(x: Any) -> Any:
    """Enum-or-str → its string value."""
    return getattr(x, "value", x)


def _lifecycle_of(final_status: Any) -> str:
    mapped = STATUS_TO_LIFECYCLE.get(final_status) or STATUS_TO_LIFECYCLE.get(
        _val(final_status)
    )
    return _val(mapped) if mapped is not None else f"<unmapped:{_val(final_status)}>"


def _dispositioned_edi_scenarios() -> List[Dict[str, Any]]:
    catalog = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    # Only EDI ``scenarios:`` carrying a UI disposition (``lifecycle``) are
    # bootstrapped through the graph. ``email_scenarios:`` use a distinct
    # intake path (out of scope for this lock).
    return [s for s in (catalog.get("scenarios") or []) if s.get("lifecycle")]


# Registered once at import — mirrors api/app.py startup so recipes see the
# same gateway stubs (erp_context, hold_status, signal data) the live
# sandbox backend serves.
register_sandbox_gateways()

_SCENARIOS = _dispositioned_edi_scenarios()


def test_catalog_has_dispositioned_scenarios() -> None:
    """Guards the lock itself: a parametrize over an empty list is a silent
    pass, which would let the consistency guarantee evaporate unnoticed."""
    assert len(_SCENARIOS) >= 30, (
        f"expected the full structured catalog (>=30 dispositioned EDI rows); "
        f"found {len(_SCENARIOS)} — did the catalog regress?"
    )


@pytest.mark.parametrize(
    "scenario",
    _SCENARIOS,
    ids=[str(s.get("id", "<?>")) for s in _SCENARIOS],
)
def test_declared_disposition_matches_graph(scenario: Dict[str, Any]) -> None:
    from api.routes.exceptions import _resolve_state

    event = _scenario_to_order_event(scenario)
    final_state = _resolve_state(GraphState(event=event, tenant_id=_TENANT), _TENANT)

    graph_intent = _val(final_state.intent) if final_state.intent else None
    graph_lifecycle = _lifecycle_of(final_state.final_status)

    assert graph_intent == scenario["intent"], (
        f"{scenario['id']}: declared intent {scenario['intent']!r} but graph "
        f"classified {graph_intent!r}"
    )
    assert graph_lifecycle == scenario["lifecycle"], (
        f"{scenario['id']}: declared lifecycle {scenario['lifecycle']!r} but "
        f"graph produced {graph_lifecycle!r} "
        f"(final_status={_val(final_state.final_status)!r})"
    )

    declared_verdict = scenario.get("shadow_verdict")
    if declared_verdict and final_state.shadow is not None:
        graph_verdict = _val(final_state.shadow.status)
        assert graph_verdict == declared_verdict, (
            f"{scenario['id']}: declared shadow_verdict {declared_verdict!r} but "
            f"graph produced {graph_verdict!r}"
        )
