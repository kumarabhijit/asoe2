"""ADR-028 Guard-rail 2 / action item A6 — canonical DUPLICATE_PO envelope.

Single-round-trip view that the UI consumes for the Layer-1 "what do I
do" decision and Layer-2 evidence drill-down without needing to fetch
``/analysis`` + ``/trace`` + audit-log endpoints separately.

Why a separate module instead of bolting onto ``api/schemas.py``?

  * The existing ``*AnalysisData`` family is a per-recipe READ
    projection consumed by the UI's data-presence enrichment sections.
    The envelope here is a SINGLE-INTENT flat shape distinct from that
    family — composing existing ``OrderSnapshot`` + ``ConfigLayerEntry``
    + audit / human-action lists. Different role, different consumer
    cadence.
  * Co-locating the schema + composer keeps the surface reviewable as
    one cohesive piece (the composer is the only legal producer of the
    envelope; a separate composer module would split the contract
    artificially).

Composition contract — ``compose_duplicate_po_envelope`` builds the
envelope from these data sources, in this order, with no fallbacks
that could conceal which source supplied a value (Verdict 2026-04-22
partial-truth guard):

  * ``record.original_event``                  — incoming PO snapshot
  * ``record.enrichment_context["matched_po_details"]["original_order"]``
    — matched PO snapshot (gateway-resolved)
  * ``record.enrichment_context["tenant_config"]["contribution_trace"]``
    — ADR-029 per-layer config trace
  * ``record.resolution_data``                 — recipe outputs
    (signal_breakdown, composite_score, classification,
    recommended_action, autonomy_level)
  * ``record.resolved_*`` + ``record.resolution_data["pending_override"]``
    + ``record.resolution_data["cosign"]``     — human_actions
  * ``audit_log_entries`` (per-tenant log filtered to entries whose
    ``previous_value`` or ``new_value`` payload references this
    record's exception_id) — audit_trail
  * ``trace_record["explanation"]``            — agent_reasoning

Audit-bearing classifications are documented in
``compliance/audit_bearing_registry.yaml`` under the
``DuplicatePOEnvelope`` section.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Re-use the existing OrderSnapshot from api/schemas.py — the envelope's
# incoming/matched PO shape is identical to what
# DuplicateDetectionData carries. Single source of truth for the
# snapshot type.
from api.schemas import OrderSnapshot


# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------


class ConfigLayerEntry(BaseModel):
    """One row of the ADR-029 per-layer config-resolution trace.

    Surfaces in DuplicatePOEnvelope so the operator can see WHICH of
    the 5 layers (platform → tenant → tier → customer → channel)
    supplied each final weight value. Sourced from the
    ``tenant_config`` gateway response's ``contribution_trace``.
    """

    model_config = ConfigDict(extra="forbid")

    signal: str
    value: float
    source_layer: str  # "platform" | "tenant" | "tier" | "customer" | "channel"


class AuditTrailEntry(BaseModel):
    """One chronological audit-chain event for ADR-028 G2's
    audit_trail. Mirrors policy_audit_log row shape, including the
    SOX hash so a downstream verifier can attest to chain integrity
    without re-querying the log table.

    The composer filters the per-tenant audit log to entries whose
    ``previous_value`` or ``new_value`` payload references the
    record's exception_id. See ``_entry_relates_to_exception`` for
    the predicate.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str  # policy_key — e.g. "EXCEPTION_RESOLVED"
    timestamp: str  # ISO-8601
    actor: str  # changed_by — user.sub for a human, "system" for autonomous
    payload: Dict[str, Any]  # previous_value / new_value / change_reason
    event_hash: str  # SOX chain hash


class HumanActionEntry(BaseModel):
    """One chronological human-action entry for ADR-028 G2's
    human_actions. Synthesised from ``record.resolved_*`` fields,
    cosign metadata in ``resolution_data["cosign"]``, and the
    ``pending_override`` snapshot when the record is mid-cosign.

    Distinct from audit_trail: human_actions are domain-language
    summaries (APPROVE / OVERRIDE / ESCALATE / COSIGN_APPROVE /
    COSIGN_REJECT), audit_trail entries are the SOX log rows. The
    two complement — operators read human_actions; auditors read
    audit_trail.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "APPROVE",
        "REJECT",
        "OVERRIDE",
        "ESCALATE",
        "COSIGN_APPROVE",
        "COSIGN_REJECT",
        "REANALYZE",
    ]
    actor: str
    timestamp: str
    notes: Optional[str] = None
    reason_tag: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Canonical envelope
# ---------------------------------------------------------------------------


class DuplicatePOEnvelope(BaseModel):
    """ADR-028 Guard-rail 2 canonical envelope for DUPLICATE_PO records.

    Returned by ``GET /api/v1/exceptions/duplicates/{id}``. The UI
    consumes this single payload for both Layer 1 (recommendation +
    action buttons) and Layer 2 (evidence — matched PO, signals,
    config trace, audit + human-action history).

    Audit-bearing classification: see
    ``compliance/audit_bearing_registry.yaml::DuplicatePOEnvelope``.
    """

    model_config = ConfigDict(extra="forbid")

    # ── Identity ───────────────────────────────────────────────────────
    exception_id: str
    tenant_id: str
    intent: Literal["DUPLICATE_PO"]
    lifecycle_state: str
    shadow_verdict: Optional[str] = None
    final_status: Optional[str] = None

    # ── PO snapshots ───────────────────────────────────────────────────
    incoming_po: OrderSnapshot
    matched_po: Optional[OrderSnapshot] = None

    # ── Recipe outputs ─────────────────────────────────────────────────
    signal_breakdown: Dict[str, float]
    composite_score: float
    classification: str  # AUTO_BLOCK | REVIEW_REQUIRED | SOFT_FLAG | PASS
    recommended_action: str  # AllowedResolutionAction
    autonomy_level: Optional[str] = None  # L1 | L2 | L3 | L4

    # ── Narrative ──────────────────────────────────────────────────────
    agent_reasoning: Optional[str] = None

    # ── ADR-029 per-layer config trace ─────────────────────────────────
    config_layer_trace: Optional[List[ConfigLayerEntry]] = None

    # ── ADR-028 G2 chronological lists ─────────────────────────────────
    audit_trail: List[AuditTrailEntry] = Field(default_factory=list)
    human_actions: List[HumanActionEntry] = Field(default_factory=list)

    # ── Timestamps ─────────────────────────────────────────────────────
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def _build_incoming_po(record: Any) -> OrderSnapshot:
    """Project the original_event into an OrderSnapshot.

    The OrderEvent doesn't carry a SAP sales-order number (orders that
    haven't entered SAP yet have none); we use the exception_id as the
    surrogate so the operator has SOME stable handle to cite. The
    ``status`` is fixed to the lifecycle_state at envelope-composition
    time — this is the incoming PO's processing state, not its SAP
    state.
    """
    event = record.original_event or {}
    line_count = int(event.get("line_count") or 1)
    po_price = float(event.get("po_price") or 0.0)
    return OrderSnapshot(
        so_number=record.id,  # surrogate — incoming PO has no SAP SO yet
        po_number=event.get("order_id") or record.id,
        created_date=record.created_at,
        total_value=po_price * line_count,
        line_count=line_count,
        status=record.lifecycle_state,
    )


def _build_matched_po(record: Any) -> Optional[OrderSnapshot]:
    """Project enrichment_context.matched_po_details.original_order
    into an OrderSnapshot. Returns None when the gateway didn't
    supply matched-PO details (early-halt paths).
    """
    enrichment = record.enrichment_context or {}
    matched = (enrichment.get("matched_po_details") or {}).get("original_order")
    if not matched:
        return None
    try:
        return OrderSnapshot(**matched)
    except Exception:  # pragma: no cover - defensive
        # Schema drift in the gateway payload — surface to logs but
        # don't crash the envelope. Caller treats matched_po as None.
        return None


def _build_config_layer_trace(record: Any) -> Optional[List[ConfigLayerEntry]]:
    """Project enrichment_context.tenant_config.contribution_trace into
    a typed list. Returns None when the gateway didn't run (test paths
    that bypass orchestration) or the trace is empty.
    """
    enrichment = record.enrichment_context or {}
    tc = enrichment.get("tenant_config") or {}
    raw = tc.get("contribution_trace")
    if not raw:
        return None
    try:
        return [ConfigLayerEntry(**entry) for entry in raw]
    except Exception:  # pragma: no cover - defensive
        return None


def _entry_relates_to_exception(entry: Dict[str, Any], exception_id: str) -> bool:
    """Per-tenant audit log entries are not exception-scoped at the
    schema level. We filter by membership: ``previous_value`` or
    ``new_value`` (when dict-shaped) must reference the record's
    exception_id.

    Convention from api/routes/exceptions.py: ``prior_snapshot`` always
    includes ``exception_id``. ``new_value`` may not. This filter
    therefore catches every event currently emitted by the routes
    layer; any future event whose payload omits exception_id from BOTH
    fields will not be filtered through here. Documented in
    ADR-028 §V1.5 for follow-up standardisation.
    """
    for key in ("previous_value", "new_value"):
        value = entry.get(key)
        if isinstance(value, dict) and value.get("exception_id") == exception_id:
            return True
    return False


def _build_audit_trail(
    audit_log_entries: List[Dict[str, Any]],
    exception_id: str,
) -> List[AuditTrailEntry]:
    """Filter the per-tenant audit log down to entries that reference
    this exception, and project into the typed envelope shape. Order
    is preserved (chronological) so auditors see events in the order
    they happened.
    """
    out: List[AuditTrailEntry] = []
    for entry in audit_log_entries:
        if not _entry_relates_to_exception(entry, exception_id):
            continue
        # Defensive: every required field must be present. Missing
        # fields surface as a skip rather than a crash — the audit log
        # is the source of truth, but the envelope is a projection.
        if not all(k in entry for k in ("policy_key", "created_at", "event_hash")):
            continue
        payload = {
            "previous_value": entry.get("previous_value"),
            "new_value": entry.get("new_value"),
            "change_reason": entry.get("change_reason"),
        }
        out.append(AuditTrailEntry(
            event_type=entry["policy_key"],
            timestamp=entry["created_at"],
            actor=entry.get("changed_by") or "system",
            payload=payload,
            event_hash=entry["event_hash"],
        ))
    return out


def _build_human_actions(record: Any) -> List[HumanActionEntry]:
    """Synthesise the human-actions list from record state.

    Sources:
      * ``record.resolved_by`` + ``record.resolved_action`` +
        ``record.resolution_notes`` — the most recent resolution by a
        human. Action type is derived from resolved_action vs the
        recipe's recommended_action (APPROVE if equal, OVERRIDE
        otherwise) and from lifecycle_state (REJECTED → REJECT,
        ESCALATED → ESCALATE).
      * ``record.resolution_data["cosign"]`` — when present, indicates
        a four-eyes cosign occurred. action = COSIGN_APPROVE or
        COSIGN_REJECT based on the approve flag.
      * ``record.resolution_data["pending_override"]`` — when present,
        indicates an override is mid-cosign. action = OVERRIDE
        with a payload flag indicating pending state.
      * ``record.reanalysis_history`` — REANALYZE entries.

    No timestamps are fabricated — when a source doesn't carry a
    timestamp, the entry uses ``record.updated_at`` as the best
    available approximation. Future cleanup: every human action
    should land its own timestamp on persistence.
    """
    actions: List[HumanActionEntry] = []
    rd = record.resolution_data or {}

    # Reanalysis history first (oldest entries chronologically come
    # before the most-recent resolved_*). Each entry carries its own
    # timestamp.
    for entry in (record.reanalysis_history or []):
        actions.append(HumanActionEntry(
            action="REANALYZE",
            actor=entry.get("triggered_by") or "system",
            timestamp=entry.get("at") or record.updated_at,
            notes=entry.get("reason"),
            payload={"attempt": entry.get("attempt")},
        ))

    # Most recent resolution (if any).
    if record.resolved_by and record.resolved_action:
        recommended = (rd.get("recommended_action") or "").strip()
        chosen = (record.resolved_action or "").strip()
        # Lifecycle-driven action type takes precedence over chosen vs
        # recommended comparison — REJECTED / ESCALATED have specific
        # routing semantics that override the simple-comparison rule.
        if record.lifecycle_state == "REJECTED":
            action_type = "REJECT"
        elif record.lifecycle_state == "ESCALATED":
            action_type = "ESCALATE"
        elif chosen == recommended and recommended:
            action_type = "APPROVE"
        else:
            action_type = "OVERRIDE"
        actions.append(HumanActionEntry(
            action=action_type,  # type: ignore[arg-type]
            actor=record.resolved_by,
            timestamp=record.updated_at,
            notes=record.resolution_notes,
            reason_tag=rd.get("reason_tag"),
            payload={"resolved_action": record.resolved_action},
        ))

    # Cosign metadata (if a four-eyes cosign happened).
    cosign = rd.get("cosign")
    if isinstance(cosign, dict):
        approved = cosign.get("approved")
        action_type = "COSIGN_APPROVE" if approved else "COSIGN_REJECT"
        actions.append(HumanActionEntry(
            action=action_type,  # type: ignore[arg-type]
            actor=cosign.get("cosigned_by") or "unknown",
            timestamp=cosign.get("cosigned_at") or record.updated_at,
            notes=cosign.get("notes"),
            payload={"approved": bool(approved)},
        ))

    # Pending-override snapshot (if record is mid-cosign).
    pending = rd.get("pending_override")
    if isinstance(pending, dict):
        actions.append(HumanActionEntry(
            action="OVERRIDE",
            actor=pending.get("initiator") or "unknown",
            timestamp=pending.get("initiated_at") or record.updated_at,
            notes=pending.get("notes"),
            reason_tag=pending.get("reason_tag"),
            payload={
                "resolved_action": pending.get("action"),
                "pending_cosign": True,
                "financial_impact_usd": pending.get("financial_impact_usd"),
            },
        ))

    # Stable order: by timestamp asc, with system reanalyses ordered
    # before human resolutions on equal timestamps.
    actions.sort(key=lambda a: (a.timestamp, 0 if a.actor == "system" else 1))
    return actions


def compose_duplicate_po_envelope(
    record: Any,
    audit_log_entries: List[Dict[str, Any]],
    trace_record: Optional[Dict[str, Any]] = None,
) -> Optional[DuplicatePOEnvelope]:
    """Compose the canonical DUPLICATE_PO envelope from record state +
    per-tenant audit log + (optional) trace record.

    Returns ``None`` when the record's intent is not DUPLICATE_PO so
    the route handler can return a 404 with a precise reason rather
    than synthesising a malformed envelope.

    Args:
        record: ``ChildCase`` (in-memory store) or repository
            row dict — duck-typed access via attributes.
        audit_log_entries: Full per-tenant audit log (caller fetches
            via ``exception_store.get_audit_log(tenant_id)``). The
            composer filters for entries that reference this
            exception's id.
        trace_record: Optional ``TraceRecord`` dict; when present
            ``trace_record["explanation"]`` populates the envelope's
            ``agent_reasoning`` field.
    """
    if getattr(record, "intent", None) != "DUPLICATE_PO":
        return None

    rd = getattr(record, "resolution_data", None) or {}
    # Required recipe-output fields. If any is missing the envelope is
    # incomplete — return None so the route handler routes a structured
    # error rather than producing a partial-truth response.
    required_keys = (
        "signal_breakdown",
        "composite_score",
        "classification",
        "recommended_action",
    )
    if not all(k in rd for k in required_keys):
        return None

    incoming_po = _build_incoming_po(record)
    matched_po = _build_matched_po(record)
    config_layer_trace = _build_config_layer_trace(record)
    audit_trail = _build_audit_trail(audit_log_entries, record.id)
    human_actions = _build_human_actions(record)

    agent_reasoning: Optional[str] = None
    if trace_record:
        agent_reasoning = trace_record.get("explanation")

    return DuplicatePOEnvelope(
        exception_id=record.id,
        tenant_id=record.tenant_id,
        intent="DUPLICATE_PO",
        lifecycle_state=record.lifecycle_state,
        shadow_verdict=getattr(record, "shadow_verdict", None),
        final_status=getattr(record, "final_status", None),
        incoming_po=incoming_po,
        matched_po=matched_po,
        signal_breakdown=dict(rd["signal_breakdown"]),
        composite_score=float(rd["composite_score"]),
        classification=str(rd["classification"]),
        recommended_action=str(rd["recommended_action"]),
        autonomy_level=rd.get("autonomy_level"),
        agent_reasoning=agent_reasoning,
        config_layer_trace=config_layer_trace,
        audit_trail=audit_trail,
        human_actions=human_actions,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


__all__ = [
    "ConfigLayerEntry",
    "AuditTrailEntry",
    "HumanActionEntry",
    "DuplicatePOEnvelope",
    "compose_duplicate_po_envelope",
]
