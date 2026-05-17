"""ADR-038 Phase H.3 — case materialisation policy.

Determines whether an inbound event opens (or attaches to) an
``OrderCase``. Per ADR-038 §7.1 / §7.2:

  * Manual Order events → eager (case opens at first event regardless
    of terminal status).
  * Automated Order events → lazy (case opens only when the event
    reaches a non-clean terminal state: MANUAL_REVIEW_REQUIRED /
    BLOCKED / FAIL_TO_HUMAN / REJECTED / AUDIT_CONTEXT_MISSING).
  * Clean Automated terminal (final_status == COMPLETE) → no case
    opened. The record persists with ``parent_case_id = None``.

This module is a pure routing helper: no I/O beyond the in-memory
``case_store`` (which is itself a thread-safe singleton).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from api.case_events import (
    TERMINAL_CASE_STATUSES,
    is_terminal_status,
    publish_case_close,
    publish_case_open,
    publish_case_update,
)
from api.store import case_store, exception_store
from contracts.models import (
    STATUS_TO_LIFECYCLE,
    OrderCase,
    OrderEvent,
    TerminalStatus,
)


# ---------------------------------------------------------------------------
# Source / source_channel inference (ADR-038 §3.1, §3.3)
# ---------------------------------------------------------------------------
#
# Mapping is intentionally conservative — events may carry an explicit
# ``source`` / ``source_channel`` in metadata (e.g., from the upstream
# email-intelligence-agent). When absent we infer from event_type using
# this table. Default falls through to ``automated_order`` /
# ``edi_x12_850`` which mirrors the V1 traffic shape.

_MANUAL_EVENT_TYPES = frozenset({
    # ADR-034 §6.2 — canonical post-cutover event_type (intent =
    # event_type per ADR-038 §10.4 channel-neutral convention).
    "MANUAL_ORDER_INTAKE",
    # ADR-034 §6.2 — legacy aliases accepted until 2026-08-12.
    # The duplicate "MANUAL_ORDER_INTAKE" that lived here pre-S14
    # was a stale forward-compat entry from the §10.4 work; the
    # canonical entry above subsumes it.
    "EMAIL_ORDER_ENTRY_REQUEST",
    "EMAIL_ORDER",
})


def derive_source_and_channel(event: OrderEvent) -> Tuple[str, str]:
    """Return ``(source, source_channel)`` for an event.

    Resolution priority:
      1. ``event.metadata['source']`` + ``event.metadata['source_channel']``
         (explicit override; trust the producer when both are present).
      2. Event-type membership in the manual allowlist.
      3. Default to ``("automated_order", "edi_x12_850")``.
    """
    meta = event.metadata or {}
    explicit_source = meta.get("source")
    explicit_channel = meta.get("source_channel")
    if isinstance(explicit_source, str) and isinstance(explicit_channel, str):
        return explicit_source, explicit_channel

    upper = (event.event_type or "").upper()
    if upper in _MANUAL_EVENT_TYPES:
        return "manual_order", "email"
    return "automated_order", "edi_x12_850"


# ---------------------------------------------------------------------------
# Case materialisation policy (ADR-038 §7.1, §7.2)
# ---------------------------------------------------------------------------


# ADR-038 §7.2 amendment (S15a follow-on, 2026-05-12).
#
# Original binding rule kept the "clean Automated COMPLETE" record
# detached from any case (parent_case_id = None, no /cases/{id}
# surface). The asoe-ui case-centric pivot (S15a) retired the
# standalone /exceptions/[id] route in favour of
# /cases/{parent}?record=<id>; without a parent, those records had
# nowhere to live. Rather than re-introduce the per-record route or
# carry a tier-1 fallback layer, the materialisation policy is
# widened: every persisted record materialises (or attaches to) a
# case. Clean-COMPLETE Automated Orders produce a case that is
# essentially "already resolved" — operators don't need to act on
# it, but it provides the canonical audit surface and the
# bookmarkable /cases/{id} URL.
#
# The legacy set is kept as a named constant so the §7.2 history
# remains greppable (and so an audit query "which terminal statuses
# were ever excluded?" has a primary source). Today every
# persisted event materialises; the set is informational.
_NON_CLEAN_TERMINAL_STATUSES = frozenset({
    TerminalStatus.MANUAL_REVIEW_REQUIRED.value,
    TerminalStatus.BLOCKED.value,
    TerminalStatus.FAIL_TO_HUMAN.value,
    TerminalStatus.REJECTED.value,
    TerminalStatus.AUDIT_CONTEXT_MISSING.value,
})


def should_materialise(
    event: OrderEvent,
    final_status: Optional[str],
) -> bool:
    """Return True when this event should open or attach to an OrderCase.

    ADR-038 §7.2 (S15a amendment 2026-05-12):
      * Every persisted record materialises a case — Manual and
        Automated alike, regardless of terminal status.

    The arguments are kept for signature stability; the body no
    longer branches on them. The named arguments help readers tying
    the call site back to the pre-amendment policy when reading
    historical commits.
    """
    # Keep the argument references live so a future "tighten this
    # back up" amendment doesn't have to re-thread them.
    _ = event
    _ = final_status
    return True


def resolve_or_open_case(
    tenant_id: str,
    event: OrderEvent,
    *,
    bundle_version_at_open: Optional[str] = None,
) -> Tuple[OrderCase, bool]:
    """Lookup-or-create the case for this event.

    Caller is responsible for checking ``should_materialise`` first;
    this helper assumes the policy decision has already been made.
    """
    # ADR-034 §6.2 — observe inbound event_type. The deprecation
    # counter trends toward zero across the transitional window;
    # the §28.6 dashboard surfaces both canonical and legacy.
    from api.metrics import record_event_type_received
    record_event_type_received(event.event_type)

    source, channel = derive_source_and_channel(event)

    meta = event.metadata or {}
    customer_po = (
        meta.get("customer_po_number")
        or event.order_id  # event.order_id IS the customer PO in V1
    )
    sales_order_id = meta.get("sales_order_id")
    edi_transaction_id = meta.get("edi_transaction_id")
    source_email_id = meta.get("source_email_id")

    return case_store.lookup_or_create(
        tenant_id=tenant_id,
        source=source,
        source_channel=channel,
        customer_id=event.retailer_id,
        customer_po_number=customer_po,
        sales_order_id=sales_order_id,
        edi_transaction_id=edi_transaction_id,
        source_email_id=source_email_id,
        bundle_version_at_open=bundle_version_at_open,
    )


# ---------------------------------------------------------------------------
# Case-status aggregation (ADR-038 §6.1)
# ---------------------------------------------------------------------------
#
# `OrderCase.status` is a roll-up of the case's child records: the
# case sits at the status of its *least-settled* child. A case with
# one resolved record and one record still awaiting a human is
# OPEN_AWAITING_HUMAN, not RESOLVED — the operator still owes a
# decision. This mirrors the asoe-ui `aggregateCaseStatus` /
# `caseFromMockException` derivation 1:1 so the mock preview layer
# and the live backend agree.
#
# Dominance order (highest wins): OPEN_AWAITING_HUMAN > BLOCKED >
# FAILED > RESOLVED. A case only reaches a terminal status once every
# child has terminal-closed.
#
# Scope note: this projection produces the four child-derivable
# statuses only. OPEN_AGENT_PROCESSING (the OrderCase default) and
# OPEN_AWAITING_BUYER / OPEN_AWAITING_ERP are set by other flows
# (just-opened pre-record, REQUEST_CLARIFICATION, ERP submit) and are
# not reachable from a persisted record's lifecycle. Cosign-parked
# cases (`pending_override` set) are owned by the cosign flow and are
# skipped entirely — see `recompute_case_status`.

_CASE_STATUS_RANK = {
    "OPEN_AWAITING_HUMAN": 3,
    "BLOCKED": 2,
    "FAILED": 1,
    "RESOLVED": 0,
}


def _case_status_from_lifecycle(lifecycle_state: Optional[str]) -> str:
    """Project one child record's ``lifecycle_state`` onto a candidate
    ``CaseStatus``. Mirrors the asoe-ui `caseFromMockException` switch
    exactly so backend and mock-preview agree.
    """
    if lifecycle_state in ("RESOLVED", "CLOSED"):
        return "RESOLVED"
    if lifecycle_state == "BLOCKED":
        return "BLOCKED"
    if lifecycle_state == "FAILED":
        return "FAILED"
    # PENDING_REVIEW / ESCALATED / PENDING_ADMIN_REVIEW / PENDING_COSIGN
    # / REJECTED / INGESTED / CLASSIFYING / AUDITING — a human (or the
    # agent) still owes forward progress on this child.
    return "OPEN_AWAITING_HUMAN"


# Sentinel — distinguishes "no incoming record" (re-aggregate the
# already-persisted children only, e.g. the disposition path) from
# "incoming record whose final_status happens to be None" (the
# materialise path always has an incoming record).
_NO_INCOMING: Any = object()


def _aggregate_case_status(
    tenant_id: str,
    case_id: str,
    incoming_final_status: Any = _NO_INCOMING,
) -> Optional[str]:
    """Return the ``CaseStatus`` aggregated from every record attached
    to ``case_id``, optionally folding in a not-yet-persisted incoming
    record.

    ``incoming_final_status`` is passed by the materialise path, where
    the event's record is created immediately *after* this call and so
    isn't in the store yet; its lifecycle is derived from the terminal
    status via ``STATUS_TO_LIFECYCLE`` — the same map
    ``exception_store.create`` applies — so the projection is identical
    whether the record is already persisted or still incoming. Callers
    re-aggregating already-persisted children (the disposition path)
    omit it.

    Returns None when the case has no children and no incoming record
    — there is nothing to aggregate.
    """
    candidates = [
        _case_status_from_lifecycle(r.lifecycle_state)
        for r in exception_store.list_by_case(tenant_id, case_id)
    ]
    if incoming_final_status is not _NO_INCOMING:
        incoming_lifecycle = STATUS_TO_LIFECYCLE.get(
            incoming_final_status or "", "INGESTED",
        )
        candidates.append(_case_status_from_lifecycle(incoming_lifecycle))
    if not candidates:
        return None
    return max(candidates, key=lambda s: _CASE_STATUS_RANK[s])


def recompute_case_status(
    tenant_id: str,
    case_id: str,
    incoming_final_status: Any = _NO_INCOMING,
    *,
    emit: bool = True,
) -> Tuple[Optional[OrderCase], bool]:
    """Recompute and persist ``OrderCase.status`` from the case's child
    records, optionally folding in an incoming event's ``final_status``.

    Returns ``(case, changed)``. ``changed`` is True only when the
    aggregated status differs from the stored one. Cosign-parked cases
    (``pending_override`` set) are returned untouched — the cosign flow
    owns the status while an override is staged.

    ``closed_at`` tracks the terminal status: a terminal aggregate
    (RESOLVED / FAILED / BLOCKED) stamps it when unset; a non-terminal
    aggregate clears it. The clear path matters for multi-issue cases
    — a case that auto-blocked on its first record and then takes a
    sibling needing review rolls back to OPEN_AWAITING_HUMAN, and a
    reopened case must not carry a stale close timestamp.

    When ``emit`` is True (the default) a real status change publishes
    the matching WSEvent — ``case_close`` for a terminal aggregate,
    ``case_update`` otherwise. Callers that emit their own opening
    signal (``materialise_for_event`` on a freshly-opened case) pass
    ``emit=False`` to avoid a duplicate event.
    """
    case = case_store.get(case_id)
    if case is None:
        return None, False
    if case.pending_override is not None:
        return case, False
    new_status = _aggregate_case_status(
        tenant_id, case_id, incoming_final_status,
    )
    if new_status is None or new_status == case.status:
        return case, False
    fields: dict[str, Any] = {"status": new_status}
    if new_status in TERMINAL_CASE_STATUSES:
        if case.closed_at is None:
            fields["closed_at"] = datetime.now(timezone.utc).isoformat()
    elif case.closed_at is not None:
        # Reopened — drop the stale close timestamp.
        fields["closed_at"] = None
    updated = case_store.update(case_id, **fields)
    if emit:
        if is_terminal_status(new_status):
            publish_case_close(updated)
        else:
            publish_case_update(updated, updated_fields=["status"])
    return updated, True


def materialise_for_event(
    tenant_id: str,
    event: OrderEvent,
    final_status: Optional[str],
    *,
    bundle_version_at_open: Optional[str] = None,
) -> Optional[OrderCase]:
    """High-level entry point for the persistence path.

    Returns the case (existing or newly opened) when the event
    materialises one; None otherwise. The harness uses the returned
    case's ``case_id`` to populate ``ExceptionRecord.parent_case_id``.
    """
    if not should_materialise(event, final_status):
        return None
    case, opened_now = resolve_or_open_case(
        tenant_id, event, bundle_version_at_open=bundle_version_at_open,
    )
    # ADR-038 §6.1 — roll the case status up from its child records.
    # This event's record persists immediately after this call, so its
    # `final_status` is folded in explicitly (it isn't in the store
    # yet). On the attach path this is how a sibling record flips the
    # parent case from RESOLVED back to OPEN_AWAITING_HUMAN.
    #
    # ADR-038 §H.6 / Phase 28.5 — a freshly-opened case emits its own
    # `case_open` below (carrying the already-recomputed status), so
    # `recompute_case_status` is told not to also emit. An attach that
    # moves the status emits `case_close` / `case_update` from inside
    # `recompute_case_status`; one that doesn't emits nothing — the
    # per-exception telemetry already covers it.
    recomputed, _changed = recompute_case_status(
        tenant_id, case.case_id, final_status, emit=not opened_now,
    )
    if recomputed is not None:
        case = recomputed
    if opened_now:
        publish_case_open(case)
    return case
