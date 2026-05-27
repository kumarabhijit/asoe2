"""PARITY-8 — retention sweeper + governance plumbing.

Per the Compliance review the sweeper is the most blast-radius
operation in the system: it operates on real customer data, runs on a
schedule, and is bulk by construction. The contract here exists to
make every step of that pipeline auditable and operator-gated.

Contract surface:

  * ``is_enabled()`` — reads ``RETENTION_SWEEPER_ENABLED`` env var.
    Default ``false`` in preprod (Compliance requirement).
  * ``RetentionSweeper.dry_run(tenant_id, as_of_unix)`` — returns a
    ``SweepPlan`` of candidate items. Plan is audit-logged. NO bytes
    deleted.
  * ``RetentionSweeper.commit_with_residency_check(...)`` — enforces
    per-tenant residency before deleting; refuses with a fail-loud
    audit event when the region violates the tenant's commitment.
  * ``SCHEDULED_RETENTION_DELETE_EVENT`` — audit event type, distinct
    from ``ATTACHMENT_ERASED`` so the audit trail differentiates bulk
    sweeper deletes from operator-triggered erasures.
  * ``resolve_identity_for_sweep(...)`` — JWT sub → Entra OID →
    ``system:service-principal`` resolution order, baked into the
    tombstone identity field.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from api.errors import ASOEError

logger = logging.getLogger("asoe.api.retention_sweeper")


# Distinct event type so bulk deletes are visible in policy_audit_log
# under a name that distinguishes them from ATTACHMENT_ERASED (which
# is the operator-triggered erasure event from PARITY-0.5).
SCHEDULED_RETENTION_DELETE_EVENT = "SCHEDULED_RETENTION_DELETE"


def is_enabled() -> bool:
    """``True`` iff the sweeper is opted in via ``RETENTION_SWEEPER_ENABLED``.

    Default ``False`` — the operator must explicitly enable. Mirrors
    Decision Q4-equivalent for the governance surface: nothing
    irreversible runs without a deliberate opt-in.
    """
    raw = (os.getenv("RETENTION_SWEEPER_ENABLED") or "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class SweepCandidate:
    attachment_id: str
    tenant_id: str
    sha256: str
    ttl_days_applied: int
    reason: str  # e.g. "RETENTION_TTL_EXPIRED"


@dataclass(frozen=True)
class SweepPlan:
    """Output of ``dry_run`` — what WOULD be deleted, not what was."""

    tenant_id: str
    as_of_unix: int
    candidates: List[SweepCandidate] = field(default_factory=list)
    committed: bool = False  # always False for a dry run


@dataclass(frozen=True)
class CommitResult:
    """Output of ``commit_with_residency_check`` — what HAPPENED."""

    tenant_id: str
    residency_ok: bool
    deleted_count: int
    blocked_count: int


def resolve_identity_for_sweep(
    *,
    jwt_sub: Optional[str],
    entra_oid: Optional[str],
    is_service_principal: bool,
) -> str:
    """Resolve the identity that authored a sweep operation.

    Resolution order is the contract from the Compliance review:
      1. JWT ``sub`` (if a human operator triggered the dry-run /
         commit through the API).
      2. Entra OID (if the operator authenticated via Entra but the
         JWT chain dropped the sub somewhere).
      3. ``system:service-principal`` (the scheduled sweeper running
         under the Container App's managed identity).

    Raises ``ValueError`` when no identity can be resolved — the
    tombstone MUST carry an identity, and a missing one is a bug.
    """
    if jwt_sub:
        return jwt_sub
    if entra_oid:
        return entra_oid
    if is_service_principal:
        return "system:service-principal"
    raise ValueError(
        "resolve_identity_for_sweep: no identity available "
        "(jwt_sub, entra_oid, is_service_principal all empty)"
    )


class RetentionSweeper:
    """Bulk-delete orchestrator. Dry-run by default; commit requires
    explicit operator opt-in (the env var plus an explicit
    ``commit_with_residency_check`` call)."""

    def dry_run(self, *, tenant_id: str, as_of_unix: int) -> SweepPlan:
        """Compute the candidate-delete set without touching bytes.

        Returns a ``SweepPlan`` the operator inspects before
        committing. The plan itself is audit-logged at the call site
        (the caller passes it into ``audit_log.log_event`` under the
        ``SCHEDULED_RETENTION_DELETE_PLAN`` event type), so dry-runs
        are visible to compliance even when the operator never
        confirms.
        """
        # Plan computation is a follow-up; the seam exists now.
        return SweepPlan(
            tenant_id=tenant_id,
            as_of_unix=as_of_unix,
            candidates=[],
            committed=False,
        )

    def commit_with_residency_check(
        self,
        *,
        tenant_id: str,
        tenant_residency_region: str,
        target_storage_region: str,
        items: List[SweepCandidate],
        sweeper_identity: str = "system:service-principal",
    ) -> CommitResult:
        """Commit the deletes after a residency check.

        Refuses with ``ASOEError`` when the target storage region does
        NOT equal the tenant's declared residency region. The bytes
        shouldn't be there in the first place; this is the
        defence-in-depth check that fails loud rather than silently
        deleting from the wrong region.

        Each candidate that resolves to an extant attachment is wiped
        via ``gateways.attachment_store.erase_attachment`` (which
        preserves the PARITY-0.5 proof-of-erasure invariant: audit row
        written BEFORE byte wipe). The bulk sweeper additionally writes
        a distinct ``SCHEDULED_RETENTION_DELETE`` audit event so the
        audit trail differentiates sweeper-initiated deletes from
        operator-triggered erasures.

        Candidates whose ``attachment_id`` no longer resolves (already
        erased, race with another sweeper) DO NOT increment
        ``deleted_count`` and DO NOT write a spurious audit row.
        """
        # Residency is a system invariant — surface it first even when
        # the sweeper is disabled. An operator inspecting the error
        # needs to see "you're about to delete from the wrong region"
        # before "the sweeper is disabled"; that diagnostic order
        # matches the blast-radius hierarchy.
        if target_storage_region != tenant_residency_region:
            logger.error(
                "retention residency violation tenant=%s declared=%s target=%s",
                tenant_id, tenant_residency_region, target_storage_region,
            )
            raise ASOEError(
                code="RESIDENCY_VIOLATION",
                message=(
                    f"Refuse to delete from {target_storage_region}: "
                    f"tenant {tenant_id} residency is "
                    f"{tenant_residency_region}."
                ),
                status_code=422,
            )
        if not is_enabled():
            raise ASOEError(
                code="RETENTION_SWEEPER_DISABLED",
                message=(
                    "Retention sweeper is disabled. Set "
                    "RETENTION_SWEEPER_ENABLED=true to opt in."
                ),
                status_code=403,
            )

        # Lazy imports — the sweeper module is import-cheap; the audit
        # + store deps only land when an actual commit fires.
        from api.store import exception_store  # noqa: PLC0415
        from gateways import attachment_store as _store  # noqa: PLC0415
        from gateways.attachment_store import erase_attachment  # noqa: PLC0415

        deleted = 0
        blocked = 0
        for candidate in items:
            try:
                tombstone = erase_attachment(
                    _store._backend,  # active backend selected at module init
                    tenant_id=candidate.tenant_id,
                    attachment_id=candidate.attachment_id,
                    erased_by=sweeper_identity,
                    reason=candidate.reason,
                )
            except Exception:  # noqa: BLE001 — chain-write failure
                # erase_attachment aborts on a chain-write failure to
                # preserve the proof-of-erasure invariant. Surface that
                # as "blocked" so the operator sees the orphan in the
                # CommitResult and can retry.
                logger.exception(
                    "retention sweeper: erase failed tenant=%s attachment=%s",
                    candidate.tenant_id, candidate.attachment_id,
                )
                blocked += 1
                continue
            if tombstone is None:
                # No-op — the attachment was already gone. Don't write
                # a SCHEDULED_RETENTION_DELETE row for a non-event.
                continue
            deleted += 1
            # Distinct sweeper-attribution row alongside the
            # ATTACHMENT_ERASED tombstone written by erase_attachment.
            # The chain handles both writes; verify_audit_chain
            # remains intact across either ordering.
            exception_store.log_audit_event(
                tenant_id=candidate.tenant_id,
                policy_key=SCHEDULED_RETENTION_DELETE_EVENT,
                previous_value={
                    "attachment_id": candidate.attachment_id,
                    "sha256": candidate.sha256,
                    "ttl_days_applied": candidate.ttl_days_applied,
                },
                new_value={
                    "attachment_id": candidate.attachment_id,
                    "sha256": candidate.sha256,
                    "ttl_days_applied": candidate.ttl_days_applied,
                    "reason": candidate.reason,
                    "swept_by": sweeper_identity,
                },
                changed_by=sweeper_identity,
                change_reason=candidate.reason,
                strict=True,
            )

        return CommitResult(
            tenant_id=tenant_id,
            residency_ok=True,
            deleted_count=deleted,
            blocked_count=blocked,
        )
