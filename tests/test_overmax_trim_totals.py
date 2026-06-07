"""OverMaxAnalysisData.trimmed_total / delta_total — server-authoritative
trim-plan roll-ups (UX-remediation T4 / Guardrail #6).

The UI section used to derive these totals with a client-side `reduce`. They
are now computed once, deterministically, in the typed contract so the
displayed figures are a single auditable source of truth. Each invariant the
`_compute_trim_totals` model_validator encodes gets a focused test.
"""

from api.schemas import OverMaxAnalysisData, TrimPlanLine


def _data(trim_plan, **overrides):
    base = dict(
        total_ordered=300.0,
        max_qty=200.0,
        excess_qty=100.0,
        exceedance_pct=50.0,
        trim_plan=trim_plan,
    )
    base.update(overrides)
    return OverMaxAnalysisData(**base)


def test_totals_are_summed_from_trim_plan():
    d = _data(
        [
            TrimPlanLine(sku="A", ordered=200, trimmed_to=150, delta=50, action="TRIM"),
            TrimPlanLine(sku="B", ordered=100, trimmed_to=50, delta=50, action="TRIM"),
        ]
    )
    assert d.trimmed_total == 200.0
    assert d.delta_total == 100.0


def test_empty_trim_plan_yields_zero_totals():
    d = _data([])
    assert d.trimmed_total == 0.0
    assert d.delta_total == 0.0


def test_inbound_totals_are_overridden_by_the_deterministic_sum():
    # A caller cannot inject a total that disagrees with the lines — the
    # validator always recomputes, so the figure can't drift.
    d = _data(
        [TrimPlanLine(sku="A", ordered=200, trimmed_to=150, delta=50)],
        trimmed_total=99999.0,
        delta_total=99999.0,
    )
    assert d.trimmed_total == 150.0
    assert d.delta_total == 50.0


def test_skip_and_ok_lines_contribute_their_recorded_values():
    d = _data(
        [
            TrimPlanLine(sku="A", ordered=200, trimmed_to=150, delta=50, action="TRIM"),
            TrimPlanLine(sku="B", ordered=80, trimmed_to=80, delta=0, action="OK"),
            TrimPlanLine(sku="C", ordered=120, trimmed_to=120, delta=0, action="SKIP"),
        ]
    )
    assert d.trimmed_total == 350.0
    assert d.delta_total == 50.0
