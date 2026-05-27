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
import os

from api.sandbox_gateways import _register_all_stub_gateways

logger = logging.getLogger("asoe.api.preprod_gateways")


def _maybe_swap_email_intake_for_graph() -> None:
    """PARITY-6.1 — replace the `email_intake` StubGateway with the
    `GraphIntakeGateway` live-or-stub router when
    ``ASOE_EMAIL_INTAKE_DRIVER=graph``.

    The router preserves the registry name ``email_intake`` so existing
    recipe ``GatewayDependency`` rows resolve unchanged. Default driver
    (``recorded`` or unset) leaves the StubGateway in place — fully
    reversible by clearing the env var per the parity plan's Rollback /
    safety net §.
    """
    driver = (os.getenv("ASOE_EMAIL_INTAKE_DRIVER") or "recorded").strip().lower()
    if driver != "graph":
        return
    try:
        from gateways.msgraph_intake import (
            GraphIntakeGateway,
            LiveGraphIntakeBackend,
        )
        from gateways.registry import get_gateway, register_gateway
    except ImportError as exc:
        logger.warning(
            "ASOE_EMAIL_INTAKE_DRIVER=graph requested but live backend "
            "imports failed (%s) — staying on stub",
            exc,
        )
        return
    try:
        existing_stub = get_gateway("email_intake")
    except KeyError:
        logger.warning(
            "ASOE_EMAIL_INTAKE_DRIVER=graph requested but no email_intake "
            "stub registered — skipping live swap",
        )
        return

    def _factory():
        return LiveGraphIntakeBackend()

    router = GraphIntakeGateway(
        stub=existing_stub, live_backend_factory=_factory,
    )
    register_gateway(router)
    logger.info(
        "email_intake gateway routed through GraphIntakeGateway "
        "(canary pct env: ASOE_CANARY_PCT_EMAIL_INTAKE)",
    )


_SAP_DOMAINS = (
    "sap_order",
    "sap_doc",
    "sap_contract",
    "promotion",
    "sap_block",
    "sap_customer_master",
    "sla_contract",
)


def _maybe_swap_sap_domains_for_live() -> None:
    """PARITY-6.3 — replace the seven SAP StubGateways with
    `SapDomainGateway` live-or-stub routers when
    ``ASOE_SAP_DRIVER=s4hana``.

    Each domain preserves its registry name so recipe
    ``GatewayDependency`` rows resolve unchanged. Default driver
    (``recorded`` or unset) leaves the seven StubGateways in place —
    fully reversible by clearing the env var per the parity plan's
    Rollback / safety net §. Live backend factory is shared across
    the seven wrappers so the OData / OAuth context is amortised.
    """
    driver = (os.getenv("ASOE_SAP_DRIVER") or "recorded").strip().lower()
    if driver != "s4hana":
        return
    try:
        from gateways.sap_live import LiveSapBackend, SapDomainGateway
        from gateways.registry import get_gateway, register_gateway
    except ImportError as exc:
        logger.warning(
            "ASOE_SAP_DRIVER=s4hana requested but live backend imports "
            "failed (%s) — staying on stubs",
            exc,
        )
        return

    # Build the live backend once and reuse it across the seven
    # wrappers (shared OAuth / OData context).
    _shared_backend: list = [None]  # closure-captured single slot

    def _factory():
        if _shared_backend[0] is None:
            _shared_backend[0] = LiveSapBackend()
        return _shared_backend[0]

    swapped: list[str] = []
    for connector in _SAP_DOMAINS:
        try:
            stub = get_gateway(connector)
        except KeyError:
            logger.warning(
                "ASOE_SAP_DRIVER=s4hana requested but %s stub not "
                "registered — skipping live swap for this domain",
                connector,
            )
            continue
        router = SapDomainGateway(
            connector=connector, stub=stub,
            live_backend_factory=_factory,
        )
        register_gateway(router)
        swapped.append(connector)

    if swapped:
        logger.info(
            "SAP domains routed through SapDomainGateway: %s "
            "(canary pct env: ASOE_CANARY_PCT_SAP)",
            ", ".join(swapped),
        )


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
    _maybe_swap_email_intake_for_graph()
    _maybe_swap_sap_domains_for_live()
