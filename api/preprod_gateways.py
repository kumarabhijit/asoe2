"""Preprod gateway registration.

Initially a thin re-use of the sandbox stub set so Azure pre-prod can
ship with the same audit-bearing UX coverage Vercel dev provides today
(per `docs/plans/azure-preprod-parity-plan.md` Phase 0b). One sub-phase
at a time, Phase 6 replaces individual `StubGateway` instances with real
Microsoft Graph / Azure Document Intelligence / SAP S/4HANA / OMS
connectors; the dispatcher in `api/app.py` swaps the call site to
`_register_all_stub_gateways` + targeted real-connector
`register_gateway(...)` calls.

Env discipline: this function is **not** env-gated — the call site
in `api/app.py` is responsible for routing only ``ASOE_ENV=preprod``
to this module. Calling it directly with a different env is the
caller's mistake, not a silent failure here.
"""
from __future__ import annotations

import logging

from api.sandbox_gateways import _register_all_stub_gateways

logger = logging.getLogger("asoe.api.preprod_gateways")


def register_preprod_gateways() -> None:
    """Register the preprod gateway set.

    Today: all stubs (parity with sandbox so the preprod UX matches the
    Vercel dev UX while real connectors are wired). Phase 6 sub-phases
    selectively replace stubs with real connectors behind the same
    operation contracts.
    """
    logger.info(
        "registering preprod gateways via stub set "
        "(Phase 6 will replace these with real connectors)",
    )
    _register_all_stub_gateways()
