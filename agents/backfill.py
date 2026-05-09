"""ADR-038 Phase H.7 — backfill of legacy ExceptionRecord rows.

Existing flat ``ExceptionRecord`` rows ship with ``parent_case_id =
None`` (T1 stateless path or pre-Phase-H.3 records). Per ADR-038
§7.7 we auto-generate an orphan ``OrderCase`` per record so the
data model is uniform; an optional second pass batch-merges
records sharing ``(tenant, customer_id, customer_po)`` onto a
single case.

This module operates on the in-memory store; the SQL-backed
equivalent lives in ``db/migrations/V011__backfill_orphan_cases.sql``
(authored alongside this commit but executed by ops on the Postgres
path). The two implementations agree on the policy: same record
→ same case_id derivation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from agents.sla import stamp_sla_deadline
from api.case_resolver import derive_source_and_channel
from api.store import case_store, exception_store
from contracts.models import OrderCase, OrderEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_from_record(record) -> Optional[OrderEvent]:
    """Reconstruct an OrderEvent from a record's persisted
    ``original_event`` payload. Returns None when the payload is
    absent (pre-Phase-19 records). Backfill skips those — they're
    orphan-only without source-channel inference."""
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
    *,
    customer_tier_lookup: Optional[Dict[str, str]] = None,
    bundle_version_at_open: str = "legacy-pre-h2",
) -> BackfillReport:
    """Materialise an OrderCase for every ExceptionRecord with
    ``parent_case_id is None``. Records with the same
    ``(tenant_id, customer_id, customer_po_number)`` attach to one
    case (correlation lookup-or-create handles this naturally).

    Returns a typed report. Idempotent: a second run is a no-op
    because every record now has ``parent_case_id`` set.

    Args:
        customer_tier_lookup: optional ``customer_id → tier_name``
            map used to compute SLA deadlines per ADR-038 Phase H.7.
            Falls back to the policy's ``default_sla_hours`` when
            absent or the customer isn't in the map.
        bundle_version_at_open: the L0 bundle version stamped on
            backfilled cases. Defaults to a sentinel that auditors
            recognise as "this case was materialised retroactively".
    """
    customer_tier_lookup = customer_tier_lookup or {}
    report = BackfillReport()

    # Iterate the in-memory record store. The DB-backed path uses
    # the same logic via the SQL companion.
    records = list(getattr(exception_store, "_records", {}).values())  # type: ignore[attr-defined]
    for record in records:
        report.records_scanned += 1
        if record.parent_case_id is not None:
            continue

        event = _event_from_record(record)
        if event is None:
            report.records_skipped_no_event += 1
            continue

        source, channel = derive_source_and_channel(event)
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

        case, opened_now = case_store.lookup_or_create(
            tenant_id=record.tenant_id,
            source=source,
            source_channel=channel,
            customer_id=event.retailer_id,
            customer_po_number=customer_po,
            sales_order_id=sales_order_id,
            edi_transaction_id=edi_transaction_id,
            source_email_id=source_email_id,
            sla_deadline=sla_deadline,
            bundle_version_at_open=bundle_version_at_open,
        )
        record.parent_case_id = case.case_id
        report.record_to_case[record.id] = case.case_id
        if opened_now:
            report.cases_opened += 1
        else:
            report.records_attached_to_existing += 1

    return report


# ---------------------------------------------------------------------------
# Pass 2 — optional merge of cases sharing (tenant, customer, customer_po)
# ---------------------------------------------------------------------------


def merge_orphan_cases_by_correlation(
    *,
    dry_run: bool = False,
) -> BackfillReport:
    """Optional second pass — when Pass 1 opened multiple cases for
    the same ``(tenant, customer_po)`` because the records arrived
    in interleaved order, merge them into one case.

    Pass 1's lookup-or-create already does this for records that
    arrived in the right order. Pass 2 catches edge cases (e.g. the
    PO field was empty on the first record but populated on the
    second).

    Phase H.7 deliberately makes Pass 2 a *separate* function. Most
    deployments never need it; running it requires a maintenance
    window because case merges affect existing references.

    Returns a report; ``dry_run=True`` makes it report-only without
    actually merging.
    """
    report = BackfillReport()

    # Group cases by (tenant, customer_po). Skip cases with no PO.
    by_key: Dict[tuple, List[OrderCase]] = {}
    for case in list(case_store._cases.values()):  # type: ignore[attr-defined]
        if not case.customer_po_number:
            continue
        key = (case.tenant_id, case.customer_po_number)
        by_key.setdefault(key, []).append(case)

    for key, cases in by_key.items():
        if len(cases) < 2:
            continue
        # Sort by opened_at — earliest case wins; later cases merge into it.
        cases_sorted = sorted(cases, key=lambda c: c.opened_at)
        primary = cases_sorted[0]
        for duplicate in cases_sorted[1:]:
            report.cases_merged += 1
            if dry_run:
                continue
            # Re-point every record that was attached to the duplicate.
            for record in getattr(exception_store, "_records", {}).values():  # type: ignore[attr-defined]
                if record.parent_case_id == duplicate.case_id:
                    record.parent_case_id = primary.case_id
                    report.record_to_case[record.id] = primary.case_id
            # Drop the duplicate case from the store.
            case_store._cases.pop(duplicate.case_id, None)  # type: ignore[attr-defined]

    return report
