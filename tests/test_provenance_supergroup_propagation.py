"""Regression: the Provenance bundle's taxonomy classification must be
populated on the persisted exception record (PresentationAudit
supergroup_code + taxonomy_version).

The bug (council 2026-06-10 cockpit verification): `_persist_exception`
materialised the parent case (which carries the taxonomy supergroup) but
called `exception_store.create(...)` WITHOUT propagating `supergroup_code`
onto the child record. `compose_presentation` reads
`record.supergroup_code`, so the Diagnostics & Audit "Provenance" card's
supergroup_code row — and the taxonomy_version it gates — were permanently
NULL on the real backend even though the classification deterministically
exists (`supergroup_for_intent(intent)`).

The store's update path already assumes supergroup_code is set at CREATE
time (it forbids reclassify-to-NULL, treating NULL as "never classified",
store §8.6), so the create path dropping it was the wiring defect.

Invariant under test: when a resolved record carries a classified intent,
the analysis Provenance bundle surfaces both supergroup_code and
taxonomy_version (Guardrail #6 — audit-bearing fields are available, not
dropped). These are pure projections of the deterministic taxonomy, so the
test needs no LLM call.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_resolved_record(monkeypatch):
    """Resolve one price-mismatch order; return (client, headers, id).

    Price-mismatch classifies deterministically (fallback provider) to a
    discriminating intent that maps to a taxonomy supergroup, so the
    Provenance bundle should carry supergroup_code + taxonomy_version.
    """
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    monkeypatch.setenv("ASOE_JWT_SECRET", "test-secret")
    import api.app as app_mod

    importlib.reload(app_mod)
    client = TestClient(app_mod.create_app())

    login = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "any"},
    )
    assert login.status_code == 200, login.text
    headers = {"authorization": f"Bearer {login.json()['access_token']}"}

    res = client.post(
        "/api/v1/exceptions/resolve",
        headers=headers,
        json={
            "order_id": "TEST-PROVENANCE-001",
            "po_price": 90.0,
            "sap_base_price": 100.0,
        },
    )
    assert res.status_code == 200, res.text
    return client, headers, res.json()["exception_id"]


def test_provenance_supergroup_and_taxonomy_version_populated(
    client_with_resolved_record,
):
    client, headers, exc_id = client_with_resolved_record

    res = client.get(f"/api/v1/exceptions/{exc_id}/analysis", headers=headers)
    assert res.status_code == 200, res.text
    audit = res.json()["presentation"]["audit"]

    # The classification deterministically exists for this intent, so the
    # Provenance card's supergroup row must be populated (not the structural
    # "unclassified" NULL state).
    assert audit["supergroup_code"], (
        "supergroup_code dropped from the persisted record — the Provenance "
        "card row is dead even though the taxonomy classification exists "
        f"(audit={audit})"
    )
    # taxonomy_version is emitted only alongside a supergroup; the two must
    # travel together (schema invariant on PresentationAudit).
    assert audit["taxonomy_version"], (
        "taxonomy_version must accompany a populated supergroup_code "
        f"(audit={audit})"
    )


def test_provenance_supergroup_matches_taxonomy_for_intent(
    client_with_resolved_record,
):
    """The surfaced supergroup is the governed taxonomy mapping for the
    record's intent — a pure projection, not a fabricated value."""
    from contracts.taxonomy import supergroup_for_intent

    client, headers, exc_id = client_with_resolved_record

    res = client.get(f"/api/v1/exceptions/{exc_id}/analysis", headers=headers)
    audit = res.json()["presentation"]["audit"]
    intent = audit["intent_code"]
    assert intent, "intent_code missing — cannot assert taxonomy projection"

    assert audit["supergroup_code"] == supergroup_for_intent(intent)
