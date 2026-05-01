"""Tests that AnalysisResponse.confidence reflects the real classifier
output, not a hardcoded sentinel.

Regression: api/routes/exceptions.py::get_analysis previously
synthesised the confidence field as ``80 if intent_selected else 0``,
ignoring the actual ``IntentDecision.confidence`` produced by the
classifier backend. The UI's AgentReasoningCard then displayed that
fabricated 80% on every exception, which made the deployed deployment
look like the LLM was returning identical confidence on every record
(a Verdict 2026-04-22 / Guardrail #6 partial-truth issue).

The fix is two-sided:
  1. Persist ``state.confidence`` into the trace_data dict so the
     read path has a source.
  2. Read ``trace_data["intent_confidence"]`` and scale 0.0-1.0 →
     0-100. Missing / zero / negative values fall back to 0 (never
     a fabricated mid-range default).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_seeded_exception(monkeypatch):
    """Run a single /resolve and return (client, token, exception_id)."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")
    monkeypatch.setenv("ASOE_JWT_SECRET", "test-secret")
    import importlib
    import api.app as app_mod

    importlib.reload(app_mod)
    client = TestClient(app_mod.create_app())

    login = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "any"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"authorization": f"Bearer {token}"}

    res = client.post(
        "/api/v1/exceptions/resolve",
        headers=headers,
        json={
            "order_id": "TEST-CONF-001",
            "po_price": 90.0,
            "sap_base_price": 100.0,
        },
    )
    assert res.status_code == 200, res.text
    return client, headers, res.json()["exception_id"]


def test_analysis_confidence_comes_from_classifier_not_hardcoded_80(
    client_with_seeded_exception,
):
    client, headers, exc_id = client_with_seeded_exception

    res = client.get(
        f"/api/v1/exceptions/{exc_id}/analysis",
        headers=headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # The deterministic fallback backend produces specific values per
    # intent (e.g. 0.90 for CONTRACTUAL_CORRECTION). Asserting one of
    # the canonical fallback confidences would couple to one intent.
    # Instead, assert the negation: confidence is NOT the legacy
    # hardcoded 80 sentinel — it's the real classifier output scaled
    # to 0-100. For the synthetic CONTRACTUAL_CORRECTION fixture the
    # fallback returns 0.90 → expect 90.
    assert body["confidence"] != 80, (
        "AnalysisResponse.confidence is still being hardcoded to 80; "
        "the read path should be projecting the real "
        "trace_data['intent_confidence'] value"
    )
    # Tighter assertion now that we know the path: deterministic
    # fallback confidence for CONTRACTUAL_CORRECTION is 0.90.
    assert body["confidence"] == 90, (
        f"Expected fallback-classifier confidence 0.90 → 90; got "
        f"{body['confidence']}"
    )


def test_analysis_confidence_zero_when_no_trace_data(monkeypatch):
    """Records that pre-date the persistence fix have no
    intent_confidence in their trace; the read path must NOT fabricate
    a 70 default (the previous code did)."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_JWT_SECRET", "test-secret")
    import importlib
    import api.app as app_mod
    from api.store import exception_store

    importlib.reload(app_mod)
    client = TestClient(app_mod.create_app())

    login = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "any"},
    )
    token = login.json()["access_token"]
    headers = {"authorization": f"Bearer {token}"}

    # Hand-craft an exception with no trace at all to exercise the
    # else-branch of get_analysis.
    record = exception_store.create(
        tenant_id="acme-corp",
        order_id="NO-TRACE-001",
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id="t-no-trace",
        intent="CONTRACTUAL_CORRECTION",
    )
    res = client.get(
        f"/api/v1/exceptions/{record.id}/analysis",
        headers=headers,
    )
    assert res.status_code == 200
    # Old behaviour: confidence=70 (fabricated default). New: 0.
    assert res.json()["confidence"] == 0


def test_analysis_confidence_handles_malformed_trace_value(monkeypatch):
    """A non-numeric / negative / zero intent_confidence in the trace
    must not crash the read path; resolves to 0 the same as
    no-trace."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_JWT_SECRET", "test-secret")
    import importlib
    import api.app as app_mod
    from api.store import exception_store

    importlib.reload(app_mod)
    client = TestClient(app_mod.create_app())

    login = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "any"},
    )
    token = login.json()["access_token"]
    headers = {"authorization": f"Bearer {token}"}

    record = exception_store.create(
        tenant_id="acme-corp",
        order_id="BAD-CONF-001",
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id="t-bad-conf",
        intent="CONTRACTUAL_CORRECTION",
    )
    # Inject a malformed trace.
    exception_store.store_trace(
        record.id,
        {
            "trace_id": "t-bad-conf",
            "event_id": "BAD-CONF-001",
            "intent_selected": "CONTRACTUAL_CORRECTION",
            "intent_confidence": "not_a_number",
            "shadow_verdict": "GREEN",
            "final_status": "COMPLETE",
        },
    )
    res = client.get(
        f"/api/v1/exceptions/{record.id}/analysis",
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["confidence"] == 0


def test_analysis_confidence_clamps_above_one(monkeypatch):
    """Defensive: a backend that accidentally writes 1.5 must not
    produce 150 in the response."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    monkeypatch.setenv("ASOE_JWT_SECRET", "test-secret")
    import importlib
    import api.app as app_mod
    from api.store import exception_store

    importlib.reload(app_mod)
    client = TestClient(app_mod.create_app())

    login = client.post(
        "/api/auth/login",
        json={"email": "marcus.webb@acme-corp.com", "password": "any"},
    )
    token = login.json()["access_token"]
    headers = {"authorization": f"Bearer {token}"}

    record = exception_store.create(
        tenant_id="acme-corp",
        order_id="HIGH-CONF-001",
        event_type="EDI_850_PRICE_MISMATCH",
        trace_id="t-high-conf",
        intent="CONTRACTUAL_CORRECTION",
    )
    exception_store.store_trace(
        record.id,
        {
            "trace_id": "t-high-conf",
            "event_id": "HIGH-CONF-001",
            "intent_selected": "CONTRACTUAL_CORRECTION",
            "intent_confidence": 1.5,
            "shadow_verdict": "GREEN",
            "final_status": "COMPLETE",
        },
    )
    res = client.get(
        f"/api/v1/exceptions/{record.id}/analysis",
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["confidence"] == 100
