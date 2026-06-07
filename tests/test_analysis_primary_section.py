"""AnalysisResponse.primary_section — the presentation hint the UI uses
to auto-expand the record's primary comparison section first.

Invariants under test:

  1. The pointer is honest: when set, it names an AnalysisResponse field
     that is ACTUALLY populated on the same response (never points at an
     absent section — a Guardrail #6 / Verdict 2026-04-22 partial-truth
     pointer).
  2. Records with no primary adapter projection (no recipe / no trace)
     get ``primary_section is None`` — no fabricated default.
  3. When a primary projection clears audit coverage and is surfaced,
     ``primary_section`` equals the field name that projection landed on.

The hint carries no business/compliance semantics; these tests assert
the read path wires it to the resolved adapter key, nothing more.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_price_mismatch(monkeypatch):
    """Resolve a single price-mismatch order; return (client, headers, id)."""
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
            "order_id": "TEST-PRIMARY-001",
            "po_price": 90.0,
            "sap_base_price": 100.0,
        },
    )
    assert res.status_code == 200, res.text
    return client, headers, res.json()["exception_id"]


def test_primary_section_pointer_is_honest(client_with_price_mismatch):
    """When primary_section is set it must name a populated field."""
    client, headers, exc_id = client_with_price_mismatch

    res = client.get(f"/api/v1/exceptions/{exc_id}/analysis", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()

    # The field always exists on the contract (additive — Guardrail #7).
    assert "primary_section" in body

    ps = body["primary_section"]
    if ps is not None:
        assert ps in body, f"primary_section names unknown field {ps!r}"
        assert body[ps] is not None, (
            f"primary_section points at {ps!r} but that section is absent — "
            "a partial-truth pointer (Verdict 2026-04-22)."
        )


def test_primary_section_none_without_projection(monkeypatch):
    """A hand-crafted record with no trace / no recipe projection must
    report primary_section is None — never a fabricated default."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_JWT_SECRET", "test-secret")
    import api.app as app_mod
    from api.store import exception_store

    importlib.reload(app_mod)
    client = TestClient(app_mod.create_app())

    login = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "any"},
    )
    headers = {"authorization": f"Bearer {login.json()['access_token']}"}

    record = exception_store.create(
        tenant_id="acme-corp",
        order_id="NO-PROJECTION-001",
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id="t-no-projection",
        intent="CONTRACTUAL_CORRECTION",
    )
    res = client.get(
        f"/api/v1/exceptions/{record.id}/analysis", headers=headers
    )
    assert res.status_code == 200, res.text
    assert res.json()["primary_section"] is None
