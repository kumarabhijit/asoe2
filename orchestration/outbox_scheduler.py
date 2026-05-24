from __future__ import annotations

# Outbox reconciliation scheduler (DoR #6).
#
# An OPT-IN, in-process periodic worker that drains the effect-outbox
# compensation queue (`orchestration.outbox.reconcile_pending`). It is OFF by
# default — set `ASOE_OUTBOX_RECONCILE_INTERVAL_S` to a positive number of
# seconds to enable it (wired into the FastAPI lifespan in `api/app.py`). When
# unset, no background task is created, so the default runtime + the test suite
# are unaffected. The `POST /api/v1/outbox/reconcile` admin endpoint remains the
# manual / external-scheduler (cron, K8s CronJob) trigger.
#
# The loop is deliberately crash-proof: a failure in one cycle is logged and the
# loop continues — a reconciler that dies on a transient error is worse than no
# reconciler.

import asyncio
import logging
import os
from typing import Awaitable, Callable, List, Optional, Sequence

from orchestration.outbox import reconcile_pending

logger = logging.getLogger("asoe.outbox.scheduler")

_ENV_INTERVAL = "ASOE_OUTBOX_RECONCILE_INTERVAL_S"


def reconcile_interval_from_env() -> Optional[float]:
    """Parse the configured interval (seconds). None/<=0 → scheduler disabled."""
    raw = os.getenv(_ENV_INTERVAL, "").strip()
    if not raw:
        return None
    try:
        interval = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; scheduler disabled", _ENV_INTERVAL, raw)
        return None
    return interval if interval > 0 else None


async def run_reconcile_loop(
    interval_s: float,
    *,
    tenant_ids: Optional[Sequence[str]] = None,
    max_cycles: Optional[int] = None,
    reconcile: Callable[..., dict] = reconcile_pending,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> List[dict]:
    """Run the reconcile loop until cancelled (or `max_cycles` for tests).

    `tenant_ids` None → a single `reconcile(tenant_id=None)` per cycle (drains
    every tenant on the in-memory backend); a list → one call per tenant (the
    DB backend is tenant-scoped). Returns the per-cycle reports (useful in
    tests). Each cycle is wrapped so an error never kills the loop.
    """
    reports: List[dict] = []
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            if tenant_ids is None:
                reports.append(reconcile(tenant_id=None))
            else:
                for tid in tenant_ids:
                    reports.append(reconcile(tenant_id=tid))
        except Exception:  # pragma: no cover - loop must survive a bad cycle
            logger.exception("outbox reconcile cycle failed")
        if max_cycles is not None and cycles >= max_cycles:
            break
        await sleep(interval_s)
    return reports


def start_if_configured(tenant_ids: Optional[Sequence[str]] = None) -> Optional[asyncio.Task]:
    """Create the background reconcile task iff the env interval is set.

    Returns the asyncio.Task (so the lifespan can cancel it on shutdown) or None
    when the scheduler is disabled. Must be called from within a running loop.
    """
    interval = reconcile_interval_from_env()
    if interval is None:
        return None
    logger.info("starting outbox reconcile scheduler: every %ss", interval)
    return asyncio.create_task(run_reconcile_loop(interval, tenant_ids=tenant_ids))
