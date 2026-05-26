"""Tests for the Playwright-fixture sandbox endpoints."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store
from gateways import attachment_store


@pytest.fixture()
def client():
    exception_store.clear()
    # Audit log clear too, so verify_audit_chain starts from GENESIS.
    if hasattr(exception_store, "_audit_log"):
        exception_store._audit_log.clear()
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_pending(client, token):
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json={
            "order_id": "PO-SB-1",
            "po_price": 100.0,
            "sap_base_price": 120.0,
            "event_type": "EDI_850_PRICE_MISMATCH",
        },
        headers=_auth(token),
    )
    return r.json()["exception_id"]


class TestSeedFinancialImpact:
    def test_attaches_financial_impact_to_existing_record(self, client):
        analyst = create_test_token(roles=["analyst"], org="tenant-a")
        manager = create_test_token(roles=["manager"], org="tenant-a")
        eid = _seed_pending(client, analyst)
        r = client.post(
            "/api/v1/_sandbox/seed/financial-impact",
            json={"exception_id": eid, "financial_impact_usd": 25_000.0},
            headers=_auth(manager),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["financial_impact_usd"] == 25_000.0
        rec = exception_store.get(eid, "tenant-a")
        assert rec.resolution_data["financial_impact_usd"] == 25_000.0

    def test_unknown_exception_returns_404(self, client):
        manager = create_test_token(roles=["manager"], org="tenant-a")
        r = client.post(
            "/api/v1/_sandbox/seed/financial-impact",
            json={"exception_id": "does-not-exist", "financial_impact_usd": 1000.0},
            headers=_auth(manager),
        )
        assert r.status_code == 404

    def test_analyst_forbidden(self, client):
        analyst = create_test_token(roles=["analyst"], org="tenant-a")
        eid = _seed_pending(client, analyst)
        r = client.post(
            "/api/v1/_sandbox/seed/financial-impact",
            json={"exception_id": eid, "financial_impact_usd": 1.0},
            headers=_auth(analyst),
        )
        assert r.status_code == 403

    def test_sandbox_endpoints_hidden_outside_sandbox_env(self, monkeypatch):
        """A production-configured app refuses to boot (PARITY-0 Phase 0b
        fail-loud) — a strictly stronger guarantee than "sandbox routes
        return 404 in production": the app never runs there at all until
        register_production_gateways is wired with real connectors."""
        import pytest as _pytest
        monkeypatch.setenv("ASOE_ENV", "production")
        with _pytest.raises(NotImplementedError):
            create_app()


class TestTenantReset:
    def test_clears_caller_tenant(self, client):
        analyst = create_test_token(roles=["analyst"], org="tenant-a")
        admin = create_test_token(roles=["admin"], org="tenant-a")
        # Produce a record and an audit event
        eid = _seed_pending(client, analyst)
        client.post(
            f"/api/v1/exceptions/{eid}/escalate",
            json={"reason": "pre-reset"},
            headers=_auth(analyst),
        )
        assert exception_store.get(eid, "tenant-a") is not None

        r = client.post(
            "/api/v1/_sandbox/tenant/reset",
            json={},
            headers=_auth(admin),
        )
        assert r.status_code == 200, r.json()
        assert r.json()["ok"] is True
        # Records and audit entries for tenant-a are gone.
        assert exception_store.get(eid, "tenant-a") is None
        tenant_a_audit = [
            e for e in getattr(exception_store, "_audit_log", [])
            if e.get("tenant_id") == "tenant-a"
        ]
        assert tenant_a_audit == []

    def test_cross_tenant_reset_forbidden(self, client):
        admin_a = create_test_token(roles=["admin"], org="tenant-a")
        r = client.post(
            "/api/v1/_sandbox/tenant/reset",
            json={"tenant_id": "tenant-b"},
            headers=_auth(admin_a),
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# ADR-043 P1.1 — seed an EMAIL_ENTRY case + stored attachment + projected
# EvidenceAnchors (docs/specs/sandbox-attachment-anchor-seed.md).
# ---------------------------------------------------------------------------

_SEED_PATH = "/api/v1/_sandbox/seed/email-attachment-anchors"

# Canonical example mirrored from the Playwright journeys (SE_EXAMPLE).
_DOC_TEXT = (
    "Purchase Order PO# EML-PO-2026-0042 - ship to Atlanta DC, "
    "need by May 24. Cola 12pk x 600."
)
_ANCHORS = [
    {"text": "PO# EML-PO-2026-0042", "label": "PO number",
     "supports_ref": "order_entry.customer_po"},
    {"text": "ship to Atlanta DC", "label": "Ship-to",
     "supports_ref": "order_entry.ship_to"},
    {"text": "need by May 24", "label": "Requested date",
     "supports_ref": "order_entry.requested_date"},
    {"text": "Cola 12pk x 600", "label": "Material",
     "supports_ref": "order_entry.material"},
]


def _seed_body(**overrides):
    body = {
        "document_text": _DOC_TEXT,
        "attachment_name": "PO_8842.pdf",
        "attachment_mime": "application/pdf",
        "anchors": _ANCHORS,
    }
    body.update(overrides)
    return body


class TestSeedEmailAttachmentAnchors:
    def test_seed_creates_located_analysis(self, client):
        attachment_store._backend.clear()
        manager = create_test_token(roles=["manager"], org="tenant-a")
        r = client.post(_SEED_PATH, json=_seed_body(), headers=_auth(manager))
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["ok"] is True
        assert body["exception_id"] and body["case_id"] and body["attachment_id"]

        rr = client.get(
            f"/api/v1/exceptions/{body['exception_id']}/analysis",
            headers=_auth(manager),
        )
        assert rr.status_code == 200, rr.json()
        analysis = rr.json()
        source = analysis.get("email_source")
        assert source is not None, (
            f"email_source missing; payload keys: {list(analysis)}"
        )
        manifest = source["attachment_manifest"]
        assert manifest and manifest[0]["attachment_id"] == body["attachment_id"]
        assert manifest[0]["sha256"]
        anchors = source["evidence_anchors"]
        assert anchors, "evidence_anchors must be populated so the UI lights up"
        for anchor in anchors:
            assert anchor["text"] in _DOC_TEXT, (
                f"anchor {anchor['text']!r} not locatable in document_text"
            )
            assert anchor["attachment_id"] == body["attachment_id"]
            assert anchor["source_sha256"] == manifest[0]["sha256"]
            assert anchor["anchor_source"] == "text_derived"

    def test_seeded_bytes_contain_document_text_pdf(self, client):
        attachment_store._backend.clear()
        manager = create_test_token(roles=["manager"], org="tenant-a")
        r = client.post(_SEED_PATH, json=_seed_body(), headers=_auth(manager))
        assert r.status_code == 200, r.json()
        body = r.json()
        dl = client.get(
            f"/api/v1/cases/{body['case_id']}/attachments/{body['attachment_id']}",
            headers=_auth(manager),
        )
        assert dl.status_code == 200, dl.text
        content = dl.content
        assert content.startswith(b"%PDF")
        assert _DOC_TEXT.encode("utf-8") in content

    def test_seeded_bytes_equal_document_text_for_text(self, client):
        attachment_store._backend.clear()
        manager = create_test_token(roles=["manager"], org="tenant-a")
        r = client.post(
            _SEED_PATH,
            json=_seed_body(attachment_name="po.csv", attachment_mime="text/csv"),
            headers=_auth(manager),
        )
        assert r.status_code == 200, r.json()
        body = r.json()
        dl = client.get(
            f"/api/v1/cases/{body['case_id']}/attachments/{body['attachment_id']}",
            headers=_auth(manager),
        )
        assert dl.status_code == 200, dl.text
        assert dl.content == _DOC_TEXT.encode("utf-8")

    def test_seed_requires_sandbox_env(self, client, monkeypatch):
        # Defense-in-depth: the router is mounted (app built under sandbox)
        # but the handler's call-time _require_sandbox() guard must still
        # 403 when ASOE_ENV flips away from sandbox.
        monkeypatch.setenv("ASOE_ENV", "production")
        manager = create_test_token(
            roles=["manager"], org="tenant-a", env="production",
        )
        r = client.post(_SEED_PATH, json=_seed_body(), headers=_auth(manager))
        assert r.status_code == 403

    def test_analyst_forbidden(self, client):
        analyst = create_test_token(roles=["analyst"], org="tenant-a")
        r = client.post(_SEED_PATH, json=_seed_body(), headers=_auth(analyst))
        assert r.status_code == 403

    def test_seed_is_tenant_scoped(self, client):
        attachment_store._backend.clear()
        manager = create_test_token(roles=["manager"], org="tenant-a")
        r = client.post(_SEED_PATH, json=_seed_body(), headers=_auth(manager))
        assert r.status_code == 200, r.json()
        attachment_id = r.json()["attachment_id"]
        # The attachment lives under the caller's tenant only.
        assert attachment_store.get_attachment("tenant-a", attachment_id) is not None
        assert attachment_store.get_attachment("tenant-b", attachment_id) is None

    def test_extra_fields_forbidden(self, client):
        manager = create_test_token(roles=["manager"], org="tenant-a")
        body = _seed_body()
        body["unexpected"] = "x"
        r = client.post(_SEED_PATH, json=body, headers=_auth(manager))
        assert r.status_code == 422
