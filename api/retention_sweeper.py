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
    ) -> CommitResult:
        """Commit the deletes after a residency check.

        Refuses with ``ASOEError`` when the target storage region does
        NOT equal the tenant's declared residency region. The bytes
        shouldn't be there in the first place; this is the
        defence-in-depth check that fails loud rather than silently
        deleting from the wrong region.
        """
        if not is_enabled():
            raise ASOEError(
                code="RETENTION_SWEEPER_DISABLED",
                message=(
                    "Retention sweeper is disabled. Set "
                    "RETENTION_SWEEPER_ENABLED=true to opt in."
                ),
                status_code=403,
            )
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
        return CommitResult(
            tenant_id=tenant_id,
            residency_ok=True,
            deleted_count=0,  # follow-up wires the actual delete
            blocked_count=0,
        )
