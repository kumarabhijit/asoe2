"""PARITY-8 — retention sweeper CLI for the Container Apps Job.

Invoked by ``infra/main.bicep::retentionSweeperJob`` on the configured
cron schedule. Reads the active tenant set from the same database the
API app uses, runs ``RetentionSweeper.dry_run`` per tenant, and — only
if ``RETENTION_SWEEPER_ENABLED=true`` — commits the plan via
``commit_with_residency_check``.

The default for ``RETENTION_SWEEPER_ENABLED`` in the bicep template is
``'false'`` so a freshly-deployed job runs as a dry-run audit until the
operator confirms the plan and flips the env var per
``docs/ops/erasure-flows.md``.

Exit codes:
  * 0 — completed (whether dry-run or commit).
  * 1 — unrecoverable error before any tenant was processed (config
        missing, DB unreachable). Container Apps surfaces a non-zero
        exit as a failed execution so the operator gets paged.

Per-tenant errors are logged and accumulated; the job exits 0 with the
final counts in the structured log so an operator can grep
``retention_sweeper_done`` for the daily summary.
"""

from __future__ import annotations

import logging
import os
import sys
import time

logger = logging.getLogger("asoe.scripts.run_retention_sweeper")


def _tenant_ids() -> list[str]:
    """Return the set of tenant ids the sweeper should walk.

    Today: a comma-separated ``ASOE_SWEEP_TENANTS`` env var (empty →
    no-op so the job runs without exploding pre-tenant-onboarding).
    GA-future: pulled from the tenant-config gateway / a runtime
    catalog (tracked in ``docs/plans/ga-preconditions.md`` row 13).
    """
    raw = (os.getenv("ASOE_SWEEP_TENANTS") or "").strip()
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _residency_for(tenant_id: str) -> tuple[str, str]:
    """Return ``(tenant_residency_region, target_storage_region)``.

    Preprod is single-region East US 2 per Decision Q6; both sides
    return the same value so the residency check passes. GA wires
    per-tenant residency from the contract-time DPA (tracked in
    ``docs/plans/ga-preconditions.md`` row 4).
    """
    region = os.getenv("ASOE_PREPROD_REGION") or "eastus2"
    return region, region


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from api.retention_sweeper import RetentionSweeper, is_enabled
    except Exception as exc:  # noqa: BLE001
        logger.error("retention_sweeper import failed: %s", exc)
        return 1

    tenants = _tenant_ids()
    if not tenants:
        logger.info(
            "retention_sweeper_done tenants=0 reason=no_tenants_configured "
            "(set ASOE_SWEEP_TENANTS to opt in)",
        )
        return 0

    sweeper = RetentionSweeper()
    as_of = int(time.time())
    commit_mode = is_enabled()
    logger.info(
        "retention_sweeper_start tenants=%d commit_mode=%s as_of=%d",
        len(tenants), commit_mode, as_of,
    )

    totals = {"planned": 0, "deleted": 0, "blocked": 0, "errors": 0}
    for tenant_id in tenants:
        try:
            plan = sweeper.dry_run(tenant_id=tenant_id, as_of_unix=as_of)
            totals["planned"] += len(plan.candidates)
            logger.info(
                "retention_sweeper_plan tenant=%s candidates=%d",
                tenant_id, len(plan.candidates),
            )
            if not commit_mode or not plan.candidates:
                continue
            tenant_region, target_region = _residency_for(tenant_id)
            result = sweeper.commit_with_residency_check(
                tenant_id=tenant_id,
                tenant_residency_region=tenant_region,
                target_storage_region=target_region,
                items=list(plan.candidates),
            )
            totals["deleted"] += result.deleted_count
            totals["blocked"] += result.blocked_count
            logger.info(
                "retention_sweeper_commit tenant=%s deleted=%d blocked=%d",
                tenant_id, result.deleted_count, result.blocked_count,
            )
        except Exception as exc:  # noqa: BLE001 — per-tenant isolation
            totals["errors"] += 1
            logger.exception(
                "retention_sweeper_tenant_error tenant=%s err=%s",
                tenant_id, exc,
            )

    logger.info(
        "retention_sweeper_done tenants=%d planned=%d deleted=%d "
        "blocked=%d errors=%d",
        len(tenants), totals["planned"], totals["deleted"],
        totals["blocked"], totals["errors"],
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
