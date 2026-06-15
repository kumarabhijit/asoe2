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


# ADR-036 — the email supergroup classifier's expected label maps to the
# governed CUSTOMER supergroup the materialised case should carry. The
# projector relays it as the ``email_supergroup_hint`` the deterministic
# backend echoes (constraints/fallback_backend.py); EMAIL_GENERAL has no
# confident CUSTOMER supergroup, so it routes to triage and the case falls
# back to the MANUAL_ORDER_INTAKE → SG_NEW_ORDER rule path.
_EXPECTED_CLASSIFICATION_TO_SUPERGROUP: Dict[str, str] = {
    "EMAIL_ORDER_ENTRY_REQUEST": "SG_NEW_ORDER",
    "EMAIL_ORDER_CHANGE_REQUEST": "SG_ORDER_CHANGE",
    "EMAIL_INQUIRY": "SG_ORDER_STATUS_INQUIRY",
    "EMAIL_COMPLAINT": "SG_COMPLAINT_SERVICE",
    "EMAIL_GENERAL": "SG_NEEDS_TRIAGE",
}

# All customer-inbox emails enter through the single MANUAL_ORDER_INTAKE
# recipe path (ADR-034 §6.2 / ADR-042). The graph classifies on this
# canonical event_type; the recipe self-routes *every* intake to review
# (free-text order intake is adversarially injectable, so it never
# auto-executes). The email *supergroup* (new order / change / inquiry /
# complaint / general) is a parallel classification carried as a hint, not
# a distinct graph event_type.
_MANUAL_INTAKE_EVENT_TYPE = "MANUAL_ORDER_INTAKE"


def _email_scenario_to_order_event(scn: Dict[str, Any]):
    """Project a catalog ``email_scenarios:`` entry onto an ``OrderEvent``.

    The email-path sibling of ``_scenario_to_order_event``. Unlike the EDI
    projector — which forwards a price payload — an inbound customer email
    carries no price, so ``po_price``/``sap_base_price`` are zeroed and the
    routing comes entirely from the ``MANUAL_ORDER_INTAKE`` event_type plus
    the metadata the ``email_intake`` gateway / ManualOrderIntakeRecipe
    consume (composite confidence + the four non-disable-able floor checks).

    The sandbox ``email_intake`` stub returns all four floor checks clear,
    so every intake deterministically lands in MANUAL_REVIEW_REQUIRED
    (PENDING_REVIEW) with a GREEN Compliance Shadow verdict — the recipe,
    not the shadow, is what holds intake back from auto-execution. That is
    the disposition the catalog declares and the consistency lock asserts.

    ``OrderEvent`` forbids extra fields, so catalog-only keys (``id`` /
    ``scenario`` / ``expected_classification`` / ``lifecycle`` / ...) are
    carried on ``metadata`` (provenance for the audit drawer + case
    materialisation), never as top-level event fields.
    """
    from contracts.models import OrderEvent

    expected = scn.get("expected_classification")
    hint = _EXPECTED_CLASSIFICATION_TO_SUPERGROUP.get(expected or "")
    # order_id IS the customer PO in V1; fall back to the catalog id for a
    # scenario with no PO (e.g. the messy no-PO fixture).
    order_id = scn.get("ref_po") or scn.get("id")

    metadata: Dict[str, Any] = {
        "composite_confidence": float(scn.get("composite_confidence") or 0.97),
        "non_disableable_floor": {
            "sender_authorized": True,
            "customer_resolved": True,
            "duplicate_po_clear": True,
            "credit_clear": True,
        },
        "validation_failures": [],
        # Provenance — read by case materialisation + the audit drawer.
        "scenario": scn.get("scenario"),
        "expected_classification": expected,
        "sender": scn.get("sender"),
        "received_at": scn.get("received_at"),
        "source_email_id": scn.get("zemail_msg_id"),
        "customer_po_number": scn.get("ref_po"),
    }
    if hint is not None:
        metadata["email_supergroup_hint"] = hint
    if scn.get("fixture") is not None:
        metadata["fixture"] = scn["fixture"]

    return OrderEvent(
        order_id=str(order_id),
        event_type=_MANUAL_INTAKE_EVENT_TYPE,
        po_price=0.0,
        sap_base_price=0.0,
        retailer_id=scn.get("retailer_id"),
        line_count=1,
        metadata=metadata,
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

    Both intake paths are materialised from the one catalog:

      * EDI ``scenarios:`` → ``_scenario_to_order_event`` (price payload).
      * ``email_scenarios:`` → ``_email_scenario_to_order_event`` (the
        MANUAL_ORDER_INTAKE customer-inbox path; no price payload).

    Each event runs through the same real graph (``_resolve_state``) and is
    persisted exactly as ``POST /api/v1/exceptions/resolve`` would, so the
    sandbox queue mirrors the asoe-ui mock generated from the same catalog.
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

    edi_scenarios = catalog.get("scenarios") or []
    email_scenarios = catalog.get("email_scenarios") or []
    created = 0

    def _materialise(scn: Dict[str, Any], to_event) -> bool:
        scenario_id = scn.get("id", "<unknown>")
        try:
            event = to_event(scn)
            state = GraphState(event=event, tenant_id=tenant_id)
            final_state = _resolve_state(state, tenant_id)
            trace_id = final_state.shadow.trace_id if final_state.shadow else None
            _persist_exception(tenant_id, final_state, trace_id)
            return True
        except Exception:  # noqa: BLE001 — one bad scenario must not abort the rest
            logger.exception("sandbox bootstrap: scenario %s failed", scenario_id)
            return False

    for scn in edi_scenarios:
        created += _materialise(scn, _scenario_to_order_event)
    for scn in email_scenarios:
        created += _materialise(scn, _email_scenario_to_order_event)

    logger.info(
        "sandbox bootstrap: created %d exception(s) from %d EDI + %d email "
        "scenario(s) in %s",
        created,
        len(edi_scenarios),
        len(email_scenarios),
        path.name,
    )
    return created
