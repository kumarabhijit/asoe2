"""ADR-038 Phase H.7 — backfill of orphan ChildCase rows.

Existing flat ``ChildCase`` rows ship with ``parent_case_id = None``
(T1 stateless path or pre-Phase-H.3 records). Per ADR-038 §7.7 we
materialise an ``OrderCase`` per record so the data model is uniform;
an optional second pass merges records sharing ``(tenant, customer_po)``
onto a single case.

Both passes operate through the **store public API** (not private
``_records`` / ``_cases`` dicts) so they work identically against the
in-memory and the DB-backed stores (Phase H.7 durable cases). The
SQL companion ``db/migrations/V011__backfill_orphan_cases.sql`` is the
Postgres-deploy equivalent of Pass 1; both agree on the policy.

Tenant-scoped: each call backfills one tenant (the DB path is
tenant-isolated via RLS / app-layer filtering, and the ops runner
iterates the tenant list). Stores are injectable for testing; they
default to the module singletons wired off ``DATABASE_URL``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents.sla import stamp_sla_deadline
from api.case_resolver import derive_origin_and_channel
from api.store import case_store as _default_case_store
from api.store import exception_store as _default_exception_store
from contracts.models import OrderEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_from_record(record) -> Optional[OrderEvent]:
    """Reconstruct an OrderEvent from a record's persisted
    ``original_event`` payload. Returns None when the payload is absent
    (pre-Phase-19 records); backfill skips those — they're orphan-only
    without source-channel inference."""
    if not record.original_event:
        return None
    try:
        return OrderEvent.model_validate(record.original_event)
    except Exception:  # noqa: BLE001 — record is dead-letter
        return None


# ---------------------------------------------------------------------------
# Backfill report
# ---------------------------------------------------------------------------


@dataclass
class BackfillReport:
    """Summary of one backfill run for the audit log."""

    records_scanned: int = 0
    cases_opened: int = 0
    records_attached_to_existing: int = 0
    records_skipped_no_event: int = 0
    cases_merged: int = 0  # only relevant when run with merge=True
    record_to_case: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pass 1 — orphan-case-per-record
# ---------------------------------------------------------------------------


def backfill_orphan_cases(
    tenant_id: str,
    *,
    customer_tier_lookup: Optional[Dict[str, str]] = None,
    bundle_version_at_open: str = "legacy-pre-h2",
    exception_store: Any = None,
    case_store: Any = None,
) -> BackfillReport:
    """Materialise an OrderCase for every orphan ChildCase in ``tenant_id``
    and persist the ``parent_case_id`` link back onto the record. Records
    sharing ``(tenant, customer_po)`` attach to one case (correlation
    lookup-or-create handles this naturally).

    Idempotent: a second run scans nothing because every record now has
    ``parent_case_id`` set (``list_orphans`` returns only NULL-parent rows).

    Args:
        tenant_id: the tenant to backfill (one call per tenant).
        customer_tier_lookup: optional ``customer_id → tier_name`` map for
            SLA-deadline computation; falls back to the policy default.
        bundle_version_at_open: L0 bundle-version sentinel stamped on
            backfilled cases (audit-recognisable as retroactive).
        exception_store / case_store: injectable stores (default: the
            module singletons). Pass both from the SAME backing store.
    """
    exc_store = exception_store or _default_exception_store
    cse_store = case_store or _default_case_store
    customer_tier_lookup = customer_tier_lookup or {}
    report = BackfillReport()

    for record in exc_store.list_orphans(tenant_id):
        report.records_scanned += 1

        event = _event_from_record(record)
        if event is None:
            report.records_skipped_no_event += 1
            continue

        origin, channel = derive_origin_and_channel(event)
        meta = event.metadata or {}
        customer_po = meta.get("customer_po_number") or event.order_id
        sales_order_id = meta.get("sales_order_id")
        edi_transaction_id = meta.get("edi_transaction_id")
        source_email_id = meta.get("source_email_id")
        tier_name = customer_tier_lookup.get(record.tenant_id) or None
        sla_deadline = stamp_sla_deadline(
            opened_at=record.created_at,
            customer_tier=tier_name,
        )

        case, opened_now = cse_store.lookup_or_create(
            tenant_id=record.tenant_id,
            origin=origin,
            source_channel=channel,
            customer_id=event.retailer_id,
            customer_po_number=customer_po,
            sales_order_id=sales_order_id,
            edi_transaction_id=edi_transaction_id,
            source_email_id=source_email_id,
            sla_deadline=sla_deadline,
            bundle_version_at_open=bundle_version_at_open,
        )
        # Persist the link (DB-backed path writes the column; in-memory
        # path mutates the record). This is the step the old in-memory-only
        # implementation skipped on the DB path.
        exc_store.update(record.id, record.tenant_id, parent_case_id=case.case_id)
        report.record_to_case[record.id] = case.case_id
        if opened_now:
            report.cases_opened += 1
        else:
            report.records_attached_to_existing += 1

    return report


# ---------------------------------------------------------------------------
# Pass 2 — optional merge of cases sharing (tenant, customer_po)
# ---------------------------------------------------------------------------


def merge_orphan_cases_by_correlation(
    tenant_id: str,
    *,
    dry_run: bool = False,
    exception_store: Any = None,
    case_store: Any = None,
) -> BackfillReport:
    """Optional second pass — when Pass 1 opened multiple cases for the
    same ``(tenant, customer_po)`` because records arrived in interleaved
    order (e.g. the PO field was empty on the first record but populated
    on the second), merge them into the earliest-opened case.

    Pass 1's lookup-or-create already dedups records that arrived in the
    right order; Pass 2 catches the edge cases. It is a SEPARATE function
    because most deployments never need it and running it requires a
    maintenance window (case merges re-point existing references and
    change downstream URLs).

    ``dry_run=True`` reports merge candidates without mutating anything.
    """
    exc_store = exception_store or _default_exception_store
    cse_store = case_store or _default_case_store
    report = BackfillReport()

    by_po: Dict[str, List[Any]] = {}
    for case in cse_store.list_by_tenant(tenant_id):
        if not case.customer_po_number:
            continue
        by_po.setdefault(case.customer_po_number, []).append(case)

    for cases in by_po.values():
        if len(cases) < 2:
            continue
        # Earliest-opened case wins; later cases merge into it.
        cases_sorted = sorted(cases, key=lambda c: c.opened_at)
        primary = cases_sorted[0]
        for duplicate in cases_sorted[1:]:
            report.cases_merged += 1
            if dry_run:
                continue
            # Re-point every child of the duplicate onto the primary, then
            # drop the now-childless duplicate case.
            for child in exc_store.list_by_case(tenant_id, duplicate.case_id):
                exc_store.update(
                    child.id, tenant_id, parent_case_id=primary.case_id,
                )
                report.record_to_case[child.id] = primary.case_id
            cse_store.delete(duplicate.case_id)

    return report
