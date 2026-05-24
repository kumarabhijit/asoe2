"""Contract test — /api/v1/_sandbox/seed/manual-order-intake (S14b).

Locks the producer-side rename cutover from ADR-034 §6.2:

  * Default emission must be event_type="MANUAL_ORDER_INTAKE" (the
    canonical, post-rename name). Producer-side tests / soaks pick
    this up automatically.
  * Setting ASOE_EMIT_LEGACY_EVENT_TYPES=1 flips emission back to
    "EMAIL_ORDER_ENTRY_REQUEST" so the dual-acceptance contract
    (test_event_type_alias_compatibility.py +
    test_deprecated_event_type_metric.py) can be exercised end-to-
    end without re-hardcoding the deprecated literal at every call
    site.
  * Both names route through the regular /exceptions/resolve graph
    and produce a valid exception_id with intent=MANUAL_ORDER_INTAKE
    — the rename is purely a producer-side concern.

When the §6.2 deadline (2026-08-12) passes, this test flips: the
legacy-emission branch will be removed and the route must reject
the flag with a clear error citing the deadline. Until then, the
test below is the standing producer-side lock.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    exception_store.clear()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def analyst_token() -> str:
    return create_test_token(roles=["analyst"], org="tenant-a")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_default_emission_is_canonical_manual_order_intake(
    client: TestClient,
    analyst_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default — no env override. Producer must emit the canonical name.
    monkeypatch.delenv("ASOE_EMIT_LEGACY_EVENT_TYPES", raising=False)

    res = client.post(
        "/api/v1/_sandbox/seed/manual-order-intake",
        json={"order_id": "EML-PO-CANONICAL"},
        headers=_auth(analyst_token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["event_type"] == "MANUAL_ORDER_INTAKE"
    assert body["emitted_legacy"] is False
    assert body["ok"] is True
    assert body["exception_id"]
    # Intent classification (deterministic fallback) must reach the
    # canonical intent regardless of which event_type literal landed.
    rec = exception_store.get(body["exception_id"], "tenant-a")
    assert rec is not None
    assert rec.intent == "MANUAL_ORDER_INTAKE"


@pytest.mark.parametrize("flag_value", ["1", "true", "yes", "TRUE", "Yes"])
def test_emit_legacy_flag_flips_to_email_order_entry_request(
    client: TestClient,
    analyst_token: str,
    monkeypatch: pytest.MonkeyPatch,
    flag_value: str,
) -> None:
    # ASOE_EMIT_LEGACY_EVENT_TYPES=1 — producer reverts to the
    # transitional name. The dual-acceptance contract guarantees this
    # still routes to intent=MANUAL_ORDER_INTAKE downstream.
    monkeypatch.setenv("ASOE_EMIT_LEGACY_EVENT_TYPES", flag_value)

    res = client.post(
        "/api/v1/_sandbox/seed/manual-order-intake",
        json={"order_id": f"EML-PO-LEGACY-{flag_value}"},
        headers=_auth(analyst_token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["event_type"] == "EMAIL_ORDER_ENTRY_REQUEST"
    assert body["emitted_legacy"] is True
    rec = exception_store.get(body["exception_id"], "tenant-a")
    assert rec is not None
    assert rec.intent == "MANUAL_ORDER_INTAKE"


@pytest.mark.parametrize("flag_value", ["", "0", "false", "no", "off", "anything-else"])
def test_falsy_flag_values_emit_canonical_name(
    client: TestClient,
    analyst_token: str,
    monkeypatch: pytest.MonkeyPatch,
    flag_value: str,
) -> None:
    # Anything outside the explicit truthy set is a no-op — defaults
    # to the canonical name. Locks the flag's parser against silent
    # drift (e.g. someone treating "0" as truthy because non-empty).
    monkeypatch.setenv("ASOE_EMIT_LEGACY_EVENT_TYPES", flag_value)

    res = client.post(
        "/api/v1/_sandbox/seed/manual-order-intake",
        json={"order_id": f"EML-PO-FALSY-{flag_value or 'empty'}"},
        headers=_auth(analyst_token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["event_type"] == "MANUAL_ORDER_INTAKE"
    assert body["emitted_legacy"] is False


def test_metadata_extra_cannot_override_reserved_routing_fields(
    client: TestClient,
    analyst_token: str,
) -> None:
    # The producer guarantees that ?confidence routes determinism:
    # the band is picked by composite_confidence + the floor checks +
    # validation_failures. Letting metadata_extra punch those keys
    # back in would let a caller divorce the routing decision from
    # the request shape that documents it. Lock the contract.
    res = client.post(
        "/api/v1/_sandbox/seed/manual-order-intake",
        json={
            "order_id": "EML-PO-RESERVED",
            "composite_confidence": 0.97,
            "metadata_extra": {
                "composite_confidence": 0.10,  # would flip to LOW band
                "non_disableable_floor": {"sender_authorized": False},
                "validation_failures": ["fake-failure"],
                "harmless_marker": "ok",
            },
        },
        headers=_auth(analyst_token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    rec = exception_store.get(body["exception_id"], "tenant-a")
    assert rec is not None
    # The 0.97 request-level confidence wins — the LOW-band override in
    # metadata_extra was filtered out. Under ADR-042 DoR #2 both bands land in
    # MANUAL_REVIEW_REQUIRED (intake never auto-executes), so the routing is
    # distinguished by the recipe classification, not the terminal status: the
    # high-confidence band yields ONE_CLICK_APPROVE; the LOW override would have
    # yielded LOW_CONFIDENCE_FLAG.
    assert rec.intent == "MANUAL_ORDER_INTAKE"
    assert rec.resolution_data.get("recommended_action") == "ONE_CLICK_APPROVE"
    assert body["final_status"] == "MANUAL_REVIEW_REQUIRED"


def test_route_rejects_outside_sandbox_env(
    client: TestClient,
    analyst_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense in depth — the include_router guard is mirrored by an
    # in-handler check. Flipping ASOE_ENV at runtime must close the
    # producer too, not just hide the route.
    monkeypatch.setenv("ASOE_ENV", "production")
    res = client.post(
        "/api/v1/_sandbox/seed/manual-order-intake",
        json={"order_id": "EML-PO-PROD"},
        headers=_auth(analyst_token),
    )
    # The route is mounted at app-creation time (sandbox mode active
    # when the fixture's app was built), but _require_sandbox()
    # re-checks at call time and returns 403.
    assert res.status_code == 403, res.text
