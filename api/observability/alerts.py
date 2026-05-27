"""PARITY-5 — pre-defined alert catalogue.

Per the Azure/SRE review: pre-declare the alert names in Phase 0 so
Phase 5 can fill in the thresholds without scope creep. Names are
stable identifiers; thresholds and notification routing live in the
Azure Monitor / App Insights side of the config.

This module exposes a typed list so dashboards, runbooks, and
on-call tooling can reference ``ALERT_CATALOGUE`` by symbol without
hand-syncing a YAML list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class AlertDescriptor:
    """A pre-declared platform alert.

    The descriptor carries the canonical name + a one-line
    description. The threshold / query / routing live in the Azure
    Monitor side (because that's where the data is).
    """

    name: str
    description: str
    severity: str  # "Sev1" | "Sev2" | "Sev3"
    runbook_anchor: str  # docs/ops/<file>.md#anchor


ALERT_CATALOGUE: List[AlertDescriptor] = [
    AlertDescriptor(
        name="zero-highlight",
        description=(
            "Spatial-overlay containment dropped to zero on a case that "
            "previously rendered highlights. Indicates AzureDI returned "
            "no anchors OR our verifier silently degraded to text mode."
        ),
        severity="Sev2",
        runbook_anchor="docs/ops/erasure-flows.md#zero-highlight",
    ),
    AlertDescriptor(
        name="layer2-open-rate",
        description=(
            "Operator Layer-2 expansion rate deviates >2σ from the "
            "trailing 7-day median. Catches a UX regression where "
            "Layer-1 stopped being self-explanatory."
        ),
        severity="Sev3",
        runbook_anchor="docs/ops/erasure-flows.md#layer2-open-rate",
    ),
    AlertDescriptor(
        name="breaker-open",
        description=(
            "A gateway circuit breaker has been open for > 5 minutes. "
            "Indicates a real upstream outage (Graph, SAP, OMS); the "
            "dead-letter queue is accumulating."
        ),
        severity="Sev1",
        runbook_anchor="docs/ops/erasure-flows.md#breaker-open",
    ),
    AlertDescriptor(
        name="extraction-cost-overrun",
        description=(
            "Per-page AzureDI cost has exceeded "
            "EXTRACTION_MAX_COST_USD_PER_PAGE on > 1% of recent calls. "
            "Provider drift OR a misrouted custom-extract caller."
        ),
        severity="Sev2",
        runbook_anchor="docs/ops/erasure-flows.md#extraction-cost-overrun",
    ),
    AlertDescriptor(
        name="audit-chain-verify-failed",
        description=(
            "verify_audit_chain() returned False — the hash-chained "
            "policy_audit_log has a broken link. P0 incident; "
            "investigate before any further appends."
        ),
        severity="Sev1",
        runbook_anchor="docs/ops/erasure-flows.md#audit-chain-verify-failed",
    ),
    AlertDescriptor(
        name="retention-sweeper-anomaly",
        description=(
            "Retention sweeper produced a delete count > 10× the "
            "trailing 7-day median, OR attempted to delete from a "
            "residency-restricted region. Sweeper is paused pending "
            "operator review."
        ),
        severity="Sev1",
        runbook_anchor="docs/ops/erasure-flows.md#retention-sweeper-anomaly",
    ),
    # PARITY-7 followup — emitted by scripts/run_drift_forwarder.py
    # via the drift-alert Container Apps Job. Severity is Sev3
    # because drift is a data-quality signal, not an operational
    # emergency (compare with breaker-open Sev1 = real outage,
    # extraction-cost-overrun Sev2 = provider drift). The 7-day
    # rolling window + 50 / 200 sample split deliberately throttles
    # the fire rate.
    AlertDescriptor(
        name="extraction-drift",
        description=(
            "7-day rolling median containment dropped >5pp on a "
            "(model_id, prompt_hash) tuple. Possible AzureDI model "
            "bump moved bounding boxes OR our golden set drifted "
            "from production traffic. Investigate before re-baselining."
        ),
        severity="Sev3",
        runbook_anchor="docs/ops/erasure-flows.md#extraction-drift",
    ),
]


def alert_names() -> List[str]:
    return [a.name for a in ALERT_CATALOGUE]
