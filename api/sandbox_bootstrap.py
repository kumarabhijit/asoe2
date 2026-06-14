"""Sandbox case bootstrap — materialise catalog scenarios into real cases.

Decision A (RFC ``asoe-ui/docs/synthetic-data-placement-rfc.md``) makes
``fixtures/scenarios/catalog.yaml`` the single declarative source the
sandbox is built from. ``tests/sandbox/seed.py`` projects the catalog's
domain entities into a SQLite double for the headless CLI / Streamlit
runners; this module is the *API-runtime* sibling: on a freshly-started
sandbox backend it runs every catalog EDI scenario through the real
Skill-Shadow-Recipe graph (``_resolve_state``) and persists the resulting
exceptions (``_persist_exception``) — the exact path ``POST
/api/v1/exceptions/resolve`` uses.

Why this matters (deployment parity)
------------------------------------
Three deployments must show the same cases, all derived from the one
catalog:

  * **local / Azure** (``NEXT_PUBLIC_USE_REAL_API=1``) — the backend
    *really* creates the cases by running the graph here.
  * **Vercel** (mock) — ``asoe-ui`` generates its mock queue from a
    committed snapshot of the same catalog.

So this module deliberately lives under ``api/`` (which ships in
``Dockerfile.api``) and carries its own catalog reader + scenario→event
projector. It does **not** import ``tests/sandbox`` — that tree is
excluded from the API image (see ``.dockerignore``).

Determinism
-----------
``run_graph`` classifies via the deterministic fallback backend when no
LLM provider is configured (the default in sandbox / CI), so the
bootstrap is reproducible without any API keys. Compliance Shadow runs
on every scenario exactly as it would for a live request — no recipe
logic is bypassed (Guardrails #1/#4).

Activation
----------
Opt-in and sandbox-only. Runs at app startup only when BOTH hold:

    ASOE_ENV=sandbox
    ASOE_SANDBOX_BOOTSTRAP=1

It is idempotent: a store that already holds records for the tenant is
left untouched (so a Postgres-backed Azure sandbox seeds once, not on
every replica restart).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger("asoe.sandbox.bootstrap")

# The catalog ships in the API image under ``fixtures/scenarios/`` (see the
# COPY in Dockerfile.api). ``api/`` is one level below the repo root, so
# parents[1] is the root.
_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "catalog.yaml"
)

DEFAULT_TENANT = "acme-corp"


def is_enabled() -> bool:
    """True when the opt-in sandbox bootstrap should run at startup."""
    return (
        os.getenv("ASOE_ENV", "production").strip().lower() == "sandbox"
        and os.getenv("ASOE_SANDBOX_BOOTSTRAP", "0").strip() == "1"
    )


def _scenario_to_order_event(scn: Dict[str, Any]):
    """Project a catalog EDI ``scenarios:`` entry onto an ``OrderEvent``.

    Mirrors ``tests/sandbox/cli.py::_edi_row_to_order_event`` so the
    bootstrap and the CLI runner build identical events from the same
    catalog rows. ``OrderEvent`` forbids extra fields, so catalog-only keys
    (``id`` / ``dc_id`` / ``origin`` / ``intent``) are intentionally not
    forwarded — they are seed/UI metadata, not event payload.
    """
    from contracts.models import OrderEvent

    meta = scn.get("metadata") or {}
    return OrderEvent(
        order_id=scn["order_id"],
        event_type=scn.get("event_type", "EDI_850_PRICE_MISMATCH"),
        sku=scn.get("sku"),
        po_price=float(scn.get("po_price") or 0.0),
        sap_base_price=float(scn.get("sap_price") or 0.0),
        retailer_id=scn.get("retailer_id"),
        line_count=int(scn.get("line_count") or 1),
        requester_role=meta.get("requester_role"),
        credit_limit=meta.get("credit_limit"),
        current_exposure=meta.get("current_exposure"),
        metadata=meta,
    )


def _store_has_records(tenant_id: str) -> bool:
    from api.store import exception_store

    page, _cursor, _more = exception_store.list(tenant_id=tenant_id, limit=1)
    return bool(page)


def bootstrap_sandbox_cases(
    *,
    tenant_id: str = DEFAULT_TENANT,
    catalog_path: Optional[Path] = None,
    force: bool = False,
) -> int:
    """Run catalog EDI scenarios through the graph and persist the results.

    Returns the number of exceptions created. Idempotent: returns 0 without
    touching the store when it already holds records for *tenant_id* (unless
    ``force=True``). Never raises — a bootstrap failure must not block the
    API from serving.

    Email scenarios (``email_scenarios:`` — MANUAL_ORDER_INTAKE) use a
    distinct intake path and carry no price payload, so they are out of
    scope here; only the EDI ``scenarios:`` are materialised.
    """
    # Imported lazily so module import has no cost / side effects outside a
    # sandbox bootstrap (mirrors the lazy gateway imports in api/app.py).
    from contracts.models import GraphState
    from api.routes.exceptions import _persist_exception, _resolve_state

    path = Path(catalog_path) if catalog_path else _CATALOG_PATH
    if not path.exists():
        logger.warning("sandbox bootstrap: catalog not found at %s; skipping", path)
        return 0

    if not force and _store_has_records(tenant_id):
        logger.info(
            "sandbox bootstrap: store already populated for tenant %s; skipping",
            tenant_id,
        )
        return 0

    try:
        catalog = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — a malformed catalog must not crash boot
        logger.exception("sandbox bootstrap: failed to parse catalog at %s", path)
        return 0

    scenarios = catalog.get("scenarios") or []
    created = 0
    for scn in scenarios:
        scenario_id = scn.get("id", "<unknown>")
        try:
            event = _scenario_to_order_event(scn)
            state = GraphState(event=event, tenant_id=tenant_id)
            final_state = _resolve_state(state, tenant_id)
            trace_id = final_state.shadow.trace_id if final_state.shadow else None
            _persist_exception(tenant_id, final_state, trace_id)
            created += 1
        except Exception:  # noqa: BLE001 — one bad scenario must not abort the rest
            logger.exception("sandbox bootstrap: scenario %s failed", scenario_id)

    logger.info(
        "sandbox bootstrap: created %d exception(s) from %d scenario(s) in %s",
        created,
        len(scenarios),
        path.name,
    )
    return created
