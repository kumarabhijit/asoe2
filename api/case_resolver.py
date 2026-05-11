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

from typing import Any, Optional, Tuple

from api.case_events import publish_case_open
from api.store import case_store
from contracts.models import OrderCase, OrderEvent, TerminalStatus


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
    "EMAIL_ORDER_ENTRY_REQUEST",
    "MANUAL_ORDER_INTAKE",
    "EMAIL_ORDER",
    "MANUAL_ORDER_INTAKE",  # forward-compat alias from ADR-038 §10.4
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


# Terminal statuses that materialise a case for Automated Orders.
# Per ADR-038 §7.2: clean COMPLETE bypasses; everything else opens.
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

    ADR-038 §7.2 binding rule:
      * Manual Order → always (eager).
      * Automated Order → only when ``final_status`` is non-clean.
      * COMPLETE / None / unknown statuses on Automated → False.
    """
    source, _ = derive_source_and_channel(event)
    if source == "manual_order":
        return True
    return (final_status or "") in _NON_CLEAN_TERMINAL_STATUSES


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
    # ADR-038 §H.6 / Phase 28.5 — emit `case_open` once per materialisation
    # so the UI's `useCases` hook can refetch and the listening client
    # sees the new case without polling. We emit only on `opened_now`;
    # subsequent events attaching to the same case are routed through
    # the per-exception telemetry already in place.
    if opened_now:
        publish_case_open(case)
    return case
