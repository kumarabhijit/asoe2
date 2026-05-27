"""OrderCase read endpoints (ADR-038 Phase H.6).

GET /api/v1/cases          — list cases for the caller's tenant, with
                              optional origin / supergroup_code / status
                              filters.
GET /api/v1/cases/{id}     — fetch a single case.

The case lifecycle is mutated implicitly by the orchestration graph
(lazy materialisation in ``api/case_resolver.py``) and the (still
dormant) Case Agent loop. This surface is read-only on purpose: any
write path that wants to alter case state must go through the
existing override / cosign / disposition flows, which Phase H.7
migrates to operate on the case lifecycle.

RBAC mirrors the exception-list endpoint: analyst, manager, admin,
viewer, and partner can read cases (partner sees only orders prefixed
by their ``retailer_id``; assigned-account users see only their
allowlist). Tenant isolation is enforced server-side via the
``get_tenant_id`` dependency — cross-tenant reads return an empty
list.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from api.deps import (
    AuthenticatedUser,
    get_current_user,
    get_tenant_id,
    require_role,
)
from api import case_intents_cache
from api.case_events import (
    is_terminal_status,
    publish_case_close,
    publish_case_update,
)
from api.errors import ASOEError
from api.metrics import record_cases_returned
from api.observability.audit_bearing import audit_bearing
from api.schemas import (
    CaseListResponse,
    CaseRecordsResponse,
    ClassificationHistoryEntry,
    ClassificationHistoryResponse,
)
from api.store import case_store, exception_store
from contracts.models import Origin

logger = logging.getLogger("asoe.api.cases")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scope_to_user(cases, user: AuthenticatedUser):
    """Apply assigned-accounts / partner-retailer scoping to the case list.

    Cases don't carry ``account_id`` directly, so we derive scope from
    the child ``ChildCase`` rows that share the case's
    ``parent_case_id``. A case is in scope when at least one of its
    children is in scope. Cases with no children yet (just-opened
    Manual Orders before any event lands) fall back to the case's
    ``customer_id`` for assigned-accounts and remain hidden from
    partner-role users (no order_id to prefix-match against).
    """
    if not user.assigned_accounts and not (
        "partner" in user.roles and user.retailer_id
    ):
        return list(cases)

    allowed_accounts = set(user.assigned_accounts or [])
    retailer_prefix = user.retailer_id if "partner" in user.roles else None

    scoped = []
    for case in cases:
        children = exception_store.list_by_case(case.tenant_id, case.case_id)
        if allowed_accounts:
            in_scope = any(
                getattr(r, "account_id", None) in allowed_accounts
                for r in children
            )
            if not in_scope and case.customer_id in allowed_accounts:
                in_scope = True
        elif retailer_prefix:
            in_scope = any(
                getattr(r, "order_id", "").startswith(retailer_prefix)
                for r in children
            )
        else:
            in_scope = True
        if in_scope:
            scoped.append(case)
    return scoped


# ---------------------------------------------------------------------------
# GET /api/v1/cases — list cases
# ---------------------------------------------------------------------------

@router.get(
    "/cases",
    response_model=CaseListResponse,
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def list_cases(
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
    origin: Optional[Origin] = Query(
        None,
        description=(
            "Filter by case origin (CUSTOMER | API). Requirements §3 "
            "glossary — CUSTOMER drives the Customer Inbox lens; API is "
            "the SAP-pushed block path. Unknown values rejected at the "
            "edge by FastAPI (constrained-input gate, CLAUDE.md §3)."
        ),
    ),
    supergroup_code: Optional[str] = Query(
        None,
        description=(
            "Filter by Intent Super-Group (requirements §6). E.g. "
            "supergroup_code=SG_BLOCK_PRICING."
        ),
    ),
    status: Optional[str] = Query(
        None,
        description=(
            "Filter by case status. Multi-value via comma-separated list "
            "(e.g. status=OPEN_AWAITING_HUMAN,OPEN_AWAITING_BUYER). "
            "Any-match: a case in any of the listed statuses passes."
        ),
    ),
    intents: Optional[str] = Query(
        None,
        description=(
            "Filter by child-record intent (Phase 28.5.x §D2). Multi-value "
            "via comma-separated list (e.g. intents=DUPLICATE_PO,CONTRACTUAL_CORRECTION). "
            "Any-match: a case with at least one child carrying one of "
            "the listed intents passes."
        ),
    ),
    since: Optional[str] = Query(
        None,
        description=(
            "Filter by recency: presets `today` / `24h` / `7d` / `30d`. "
            "Cases opened within the window pass. Anything else falls "
            "through unfiltered."
        ),
    ),
    q: Optional[str] = Query(
        None,
        description=(
            "Free-text fuzzy match over case_id / customer_po_number / "
            "sales_order_id / customer_id. Case-insensitive substring. "
            "Operators (po:, so:, customer:) are parsed client-side; the "
            "backend sees the free-text term only."
        ),
    ),
    limit: int = Query(200, ge=1, le=500),
    cursor: Optional[str] = Query(
        None,
        description=(
            "Pagination cursor. Opaque page-anchor token; clients loop "
            "`do { fetch } while (cursor)` until `has_more` is False. "
            "ADR-038 §D7 amendment (2026-05-11) — adds cursor pagination "
            "to /cases so the case-projected exception queue is no longer "
            "silently capped at the first page."
        ),
    ),
) -> CaseListResponse:
    cases = case_store.list_by_tenant(tenant_id)

    if origin:
        cases = [c for c in cases if c.origin == origin]

    if supergroup_code:
        cases = [c for c in cases if c.supergroup_code == supergroup_code]

    # Multi-value status: any-match. The empty list (after parse) is
    # treated as "no filter" so a trailing comma in the URL doesn't
    # silently zero the list.
    if status:
        status_set = {s.strip() for s in status.split(",") if s.strip()}
        if status_set:
            cases = [c for c in cases if c.status in status_set]

    # Multi-value intent filter — consults the per-case intents cache
    # (api/case_intents_cache.py). Empty after parse → no filter.
    if intents:
        intent_set = frozenset(
            i.strip() for i in intents.split(",") if i.strip()
        )
        if intent_set:
            cases = [
                c for c in cases
                if case_intents_cache.matches_any(
                    tenant_id, c.case_id, intent_set,
                )
            ]

    if since:
        cutoff = _since_cutoff(since)
        if cutoff is not None:
            cases = [c for c in cases if c.opened_at >= cutoff]

    if q:
        needle = q.strip().lower()
        if needle:
            cases = [c for c in cases if _matches_q(c, needle)]

    cases = _scope_to_user(cases, user)
    # Stable sort (opened_at DESC, case_id DESC tiebreak) — cursor
    # determinism depends on a total ordering, so a unique tiebreaker
    # is required. case_id is UUID-shaped, never collides.
    cases.sort(key=lambda c: (c.opened_at, c.case_id), reverse=True)
    total = len(cases)

    # Cursor-anchored slicing (ADR-038 §D7 amendment). The cursor is
    # the case_id of the last item returned on the previous page; we
    # advance past it in the sorted list. Unknown cursors fall through
    # silently (treated as start-of-list) — matches the
    # exception_store.list grandfathering for stale cursor tokens.
    if cursor:
        try:
            start = next(i for i, c in enumerate(cases) if c.case_id == cursor) + 1
        except StopIteration:
            start = 0
    else:
        start = 0
    page = cases[start : start + limit]
    has_more = (start + limit) < total
    next_cursor = page[-1].case_id if (has_more and page) else None

    # Phase 28.5.x §D7 — track payload size for the pagination
    # re-open trigger. We record the size of the response (page),
    # NOT the post-filter total, because the alert is about render
    # cost on the client, not about tenant case volume.
    record_cases_returned(len(page))
    return CaseListResponse(
        items=[_serialise_case(c, tenant_id) for c in page],
        total=total,
        cursor=next_cursor,
        has_more=has_more,
    )


def _since_cutoff(since: str) -> Optional[str]:
    """Map a preset (`today` / `24h` / `7d` / `30d`) to an ISO 8601
    cutoff string. Returns None for unrecognised input so the filter
    falls through unfiltered (Compliance veto on silent truncation).
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    preset = since.strip().lower()
    delta_map = {
        "today": timedelta(days=1),
        "24h":   timedelta(hours=24),
        "7d":    timedelta(days=7),
        "30d":   timedelta(days=30),
    }
    delta = delta_map.get(preset)
    if delta is None:
        return None
    return (now - delta).isoformat()


def _matches_q(case, needle: str) -> bool:
    """Free-text substring match across the four searchable fields."""
    for value in (
        case.case_id,
        case.customer_po_number,
        case.sales_order_id,
        case.customer_id,
    ):
        if value and needle in value.lower():
            return True
    return False


def _serialise_case(case, tenant_id: str) -> dict:
    """Project an OrderCase to the wire shape, plus the
    Phase 28.5.x §D2 derived `child_intents` field. Derived fields
    are added to the response dict, NOT to the OrderCase model
    (which is `extra="forbid"`); the UI's typed contract widens to
    accept them via a separate response interface.
    """
    payload = case.model_dump(mode="json")
    payload["child_intents"] = sorted(
        case_intents_cache.intents_for(tenant_id, case.case_id),
    )
    return payload


# ---------------------------------------------------------------------------
# GET /api/v1/cases/{id} — case detail
# ---------------------------------------------------------------------------

@router.get(
    "/cases/{case_id}",
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def get_case(
    case_id: str,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001
) -> dict:
    # Visibility is tenant-scoped, matching GET /api/v1/exceptions/{id}
    # (S15a alignment 2026-05-12). The case-list endpoint still
    # applies `_scope_to_user` as a UX filter — analysts see only
    # cases tied to their assigned accounts in the queue — but the
    # per-record detail view inherits visibility from the child
    # exception: if a user can open the exception (any role in the
    # tenant), they can open the parent case. Pre-amendment the two
    # endpoints disagreed, which broke deep-links into cases the
    # analyst could already see records for.
    case = case_store.get(case_id)
    if case is None or case.tenant_id != tenant_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Case {case_id} not found.",
            status_code=404,
        )
    return _serialise_case(case, tenant_id)


# ---------------------------------------------------------------------------
# GET /api/v1/cases/{id}/records — attached-record loader
# ---------------------------------------------------------------------------
#
# Phase 28.5.x §28.5 follow-up — populates the CaseDetailPanel's
# "Attached records" stack and the aggregated `policyHits` surface
# the L1-vs-L2 PolicyHitBadge section consumes. The endpoint reads
# from the existing `exception_store.list_by_case` join key
# (`ChildCase.parent_case_id`); no new persistence is needed.
#
# RBAC inherits from `_scope_to_user` — a caller who can read the
# parent case can read its children. Partner / assigned-account
# scope is enforced at the parent level (the child list will be
# subset-filtered anyway because the case already passed scope).

@router.get(
    "/cases/{case_id}/records",
    response_model=CaseRecordsResponse,
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def list_case_records(
    case_id: str,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> CaseRecordsResponse:
    case = case_store.get(case_id)
    if case is None or case.tenant_id != tenant_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Case {case_id} not found.",
            status_code=404,
        )
    # Visibility inherits from the parent case detail
    # (S15a alignment 2026-05-12). Tenant scope is sufficient — if
    # the caller can hit /cases/{id} they can enumerate the
    # attached records. The case-list endpoint is still
    # `_scope_to_user`-filtered so the queue narrows to a
    # partner / analyst's assigned scope.
    _ = user  # kept on the dependency for future per-role hardening
    records = exception_store.list_by_case(tenant_id, case_id)
    # Stable order — most-recently-updated first so the operator's
    # eye lands on the row that just changed.
    records.sort(
        key=lambda r: r.updated_at or r.created_at, reverse=True,
    )

    # Aggregate `shadow_policy_hits` across children. Dedupe while
    # preserving insertion order so the UI's badge list is stable
    # across refetches (PolicyHitBadge keys on the string itself —
    # an unstable order would re-mount every badge on every event).
    # The policy hits live on the persisted trace
    # (`shadow_policy_hits`), not on ChildCase itself, so we
    # walk traces for each child.
    seen: set[str] = set()
    aggregated: list[str] = []
    for record in records:
        trace = exception_store.get_trace(record.id) or {}
        for hit in trace.get("shadow_policy_hits") or []:
            if not isinstance(hit, str) or hit in seen:
                continue
            seen.add(hit)
            aggregated.append(hit)

    return CaseRecordsResponse(
        items=[record.to_detail().model_dump(mode="json") for record in records],
        total=len(records),
        aggregated_policy_hits=aggregated,
    )


# ---------------------------------------------------------------------------
# Classification audit trail (requirements §8.6, acceptance criterion #9)
# ---------------------------------------------------------------------------

@router.get(
    "/cases/{case_id}/classification-history",
    response_model=ClassificationHistoryResponse,
    dependencies=[Depends(require_role(
        "analyst", "manager", "admin", "viewer", "partner",
    ))],
)
async def list_classification_history(
    case_id: str,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ClassificationHistoryResponse:
    """Return the case's full classification audit trail in append order.

    Requirements §8.6: every classification or reclassification event
    appends exactly one row stamped with the taxonomy version in effect
    at the time. The audit is append-only on both sides (in-memory and
    V020 DB triggers). Reads are open to any role that can see the case.
    """
    case = case_store.get(case_id)
    if case is None or case.tenant_id != tenant_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Case {case_id} not found.",
            status_code=404,
        )
    events = case_store.get_classification_history(case_id)
    items = [
        ClassificationHistoryEntry(**event.model_dump()) for event in events
    ]
    # Partners are external retailers; ``reason_text`` is operator-authored
    # free text and may carry internal commercial notes ("escalate to VP",
    # "watchlist customer", etc.). Redact it for the partner role; analysts
    # / managers / admins / viewers (all internal) see the full row.
    if "partner" in (user.roles or ()):
        items = [it.redact_for_partner() for it in items]
    return ClassificationHistoryResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# ADR-040 X.0 — case-level four-eyes / cosign (flag-gated, default off)
# ---------------------------------------------------------------------------
#
# These two endpoints land the case-level cosign control behind
# `ASOE_CASE_COSIGN_ENABLED`. With the flag off, both endpoints
# return 404 — the route table doesn't even register them. With the
# flag on, the endpoints behave per ADR-040 §2 truth table.
#
# X.1 ratification = config flip in the live ConfigMap. No code
# change needed at flip time.

import os as _os
from datetime import datetime, timezone as _tz
from typing import List as _List

from pydantic import BaseModel, Field, ConfigDict


def _case_cosign_enabled() -> bool:
    """ADR-040 §3.1 X.0 toggle. Truthy values: 1 / true / yes."""
    return _os.getenv("ASOE_CASE_COSIGN_ENABLED", "").strip().lower() in (
        "1", "true", "yes",
    )


class CaseOverrideInitRequest(BaseModel):
    """POST /api/v1/cases/{id}/override — initiate a case-level
    override that may require a cosigner."""

    model_config = ConfigDict(extra="forbid")

    pending_action: str
    pending_reason_tag: Optional[str] = None
    aggregate_financial_impact_usd: float = 0.0
    child_exception_ids: _List[str] = Field(default_factory=list)
    notes: Optional[str] = None


class CaseCosignRequest(BaseModel):
    """POST /api/v1/cases/{id}/override/cosign — second-reviewer
    decision."""

    model_config = ConfigDict(extra="forbid")

    approve: bool
    notes: str


def _utc_now_iso() -> str:
    return datetime.now(_tz.utc).isoformat()


# ADR-040 §2.1 — same threshold as the exception-level flow.
HIGH_VALUE_OVERRIDE_THRESHOLD_USD = 10_000.0


@router.post(
    "/cases/{case_id}/override",
    dependencies=[Depends(require_role("manager", "admin"))],
)
@audit_bearing(reason="initiates a case-level financial override; SOX")
async def initiate_case_override(
    case_id: str,
    req: CaseOverrideInitRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Initiate a case-level override. ADR-040 §2.1: above the
    threshold the case enters PENDING_COSIGN with a recorded
    `pending_override`; below the threshold it would be applied
    immediately, but the immediate-apply path is X.2 work — for
    X.0 the endpoint always parks the override in PENDING_COSIGN.
    """
    if not _case_cosign_enabled():
        raise ASOEError(
            code="NOT_FOUND",
            message="Case-level cosign is not enabled in this deployment.",
            status_code=404,
        )
    case = case_store.get(case_id)
    if case is None or case.tenant_id != tenant_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Case {case_id} not found.",
            status_code=404,
        )
    from contracts.models import CasePendingOverride

    pending = CasePendingOverride(
        initiator=user.sub,
        initiated_at=_utc_now_iso(),
        pending_action=req.pending_action,
        pending_reason_tag=req.pending_reason_tag,
        aggregate_financial_impact_usd=req.aggregate_financial_impact_usd,
        child_exception_ids=list(req.child_exception_ids),
        notes=req.notes,
    )
    try:
        updated = case_store.set_pending_override(case_id, pending)
    except ValueError as exc:
        raise ASOEError(
            code="CONFLICT",
            message=str(exc),
            status_code=409,
        ) from exc
    # ADR-038 §H.6 — propagate the status flip (→ OPEN_AWAITING_HUMAN)
    # + pending_override attachment to live listeners so the case list
    # surfaces the "needs cosign" affordance without polling.
    publish_case_update(
        updated, updated_fields=["status", "pending_override"],
    )
    return updated.model_dump(mode="json")


@router.post(
    "/cases/{case_id}/override/cosign",
    dependencies=[Depends(require_role("manager", "admin"))],
)
@audit_bearing(reason="cosigns a pending case override; financial commit; SOX")
async def cosign_case_override(
    case_id: str,
    req: CaseCosignRequest,
    tenant_id: str = Depends(get_tenant_id),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """ADR-040 §2.2 — second-reviewer decision on a case-level
    override.

    Outcomes:
      * `approve=True`  → clear `pending_override`, transition
                          status → RESOLVED.
      * `approve=False` → clear `pending_override`, restore status
                          → OPEN_AGENT_PROCESSING.

    SoD: cosigner must not be the initiator. Notes mandatory.
    """
    if not _case_cosign_enabled():
        raise ASOEError(
            code="NOT_FOUND",
            message="Case-level cosign is not enabled in this deployment.",
            status_code=404,
        )
    case = case_store.get(case_id)
    if case is None or case.tenant_id != tenant_id:
        raise ASOEError(
            code="NOT_FOUND",
            message=f"Case {case_id} not found.",
            status_code=404,
        )
    if case.pending_override is None:
        raise ASOEError(
            code="NO_PENDING_OVERRIDE",
            message=(
                f"Case {case_id} carries no pending_override; "
                "nothing to cosign."
            ),
            status_code=409,
        )
    if case.pending_override.initiator == user.sub:
        raise ASOEError(
            code="SOD_VIOLATION",
            message=(
                "Segregation of duties: the user who initiated the "
                "case-level override cannot cosign their own action."
            ),
            status_code=403,
        )
    if not req.notes or not req.notes.strip():
        raise ASOEError(
            code="NOTES_REQUIRED",
            message="Cosign notes are mandatory (SOX audit requirement).",
            status_code=422,
        )
    if req.approve:
        case_store.clear_pending_override(case_id)
        # Stamp closed_at so `publish_case_close` carries the
        # transition timestamp consumers rely on.
        updated = case_store.update(
            case_id, status="RESOLVED", closed_at=_utc_now_iso(),
        )
        # Terminal status → `case_close` (not `case_update`).
        # Subscribers can drop the case from their open-list view.
        publish_case_close(updated)
        return updated.model_dump(mode="json")
    case_store.clear_pending_override(case_id)
    updated = case_store.update(case_id, status="OPEN_AGENT_PROCESSING")
    # Rejection puts the case back into the agent-processing state
    # — visible to the case list as a status flip, but not terminal.
    publish_case_update(updated, updated_fields=["status", "pending_override"])
    return updated.model_dump(mode="json")
