"""Case-level WSEvent emission helpers (ADR-038 §H.6 / Phase 28.5).

Wraps `api.events.WSEvent.case_*` factories + `api.pubsub.event_publisher`
into three small one-liners callers use after a case-state mutation:

    publish_case_open(case)              # case just materialised
    publish_case_update(case, fields)    # status / sla / working_memory change
    publish_case_close(case)             # terminal status reached

Single-responsibility: this module knows the contract between the
CaseStore mutation sites and the in-tenant pub/sub channel; the
store itself stays unaware of the WS surface (keeping the
persistence layer decoupled from the transport layer).

A failure to publish must NEVER block the underlying mutation. The
in-memory PubSub used by tests always succeeds; the Redis-backed
one logs + swallows the error (see `api.pubsub.RedisPubSub.publish`).
Callers therefore never need to wrap these helpers in try/except.
"""
from __future__ import annotations

from typing import Iterable, Optional
from uuid import uuid4

from api import case_intents_cache
from api.events import WSEvent
from api.pubsub import event_publisher
from contracts.models import OrderCase

# ADR-038 §6.1 — terminal case statuses. Reaching one of these is
# the trigger for a `case_close` event.
TERMINAL_CASE_STATUSES: frozenset[str] = frozenset({
    "RESOLVED",
    "FAILED",
    "BLOCKED",
})


def publish_case_open(
    case: OrderCase,
    *,
    trace_id: Optional[str] = None,
) -> None:
    """Emit `case_open` for a freshly-materialised case.

    Callers: `case_resolver.materialise_for_event` after a new case
    is opened (i.e. `lookup_or_create` returned `opened_now=True`).
    """
    event = WSEvent.case_open(
        trace_id=trace_id or str(uuid4()),
        case_id=case.case_id,
        tenant_id=case.tenant_id,
        source=case.source,
        source_channel=case.source_channel,
        status=case.status,
        sla_deadline=case.sla_deadline,
        customer_po_number=case.customer_po_number,
        sales_order_id=case.sales_order_id,
    )
    event_publisher.publish(case.tenant_id, event)
    # Phase 28.5.x §D2 — invalidate the per-case intents cache so
    # the next /api/v1/cases read re-derives from the exception
    # store. The case has no children at the moment of open, but
    # opening + first-event-persist happen in close succession;
    # invalidating here means the cache entry is correct on the
    # first request after the materialise.
    case_intents_cache.invalidate(case.tenant_id, case.case_id)


def publish_case_update(
    case: OrderCase,
    updated_fields: Iterable[str],
    *,
    trace_id: Optional[str] = None,
) -> None:
    """Emit `case_update` for a case that changed at least one field
    the UI cares about (status, sla_deadline, working_memory_summary,
    pending_override).

    `updated_fields` is informational — the UI uses it to decide
    whether to silently re-fetch or to merge in-place. Keep it
    accurate so the client side-effect matches reality.
    """
    event = WSEvent.case_update(
        trace_id=trace_id or str(uuid4()),
        case_id=case.case_id,
        tenant_id=case.tenant_id,
        status=case.status,
        updated_fields=list(updated_fields),
        sla_deadline=case.sla_deadline,
    )
    event_publisher.publish(case.tenant_id, event)
    # Phase 28.5.x §D2 — every case_update is a potential
    # child-set mutation (a new exception just attached, or an
    # existing one was reclassified). Invalidate the per-case
    # intents cache so the filter chips see the new intent on the
    # next /api/v1/cases read.
    case_intents_cache.invalidate(case.tenant_id, case.case_id)


def publish_case_close(
    case: OrderCase,
    *,
    trace_id: Optional[str] = None,
) -> None:
    """Emit `case_close` for a case that just reached a terminal
    status. The caller must have set `case.closed_at` to the
    transition timestamp before invoking this; if `case.closed_at`
    is None, the current case status is used as the close marker
    and `closed_at` falls back to an empty string (the consumer
    contract treats it as "unknown, defer to status field").
    """
    event = WSEvent.case_close(
        trace_id=trace_id or str(uuid4()),
        case_id=case.case_id,
        tenant_id=case.tenant_id,
        status=case.status,
        closed_at=case.closed_at or "",
    )
    event_publisher.publish(case.tenant_id, event)


def is_terminal_status(status: Optional[str]) -> bool:
    """ADR-038 §6.1 — predicate for `RESOLVED / FAILED / BLOCKED`."""
    return status in TERMINAL_CASE_STATUSES
