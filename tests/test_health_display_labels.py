"""ADR-045 — /health exposes operator display labels + the fan-out map.

The UI must source human-readable strings for taxonomy codes at runtime
from `/api/v1/health.display_labels`, never from a hand-authored UI label
map (asoe-ui Guardrail #2). Before ADR-045 the UI mislabelled the
`MANUAL_ORDER_INTAKE` intent as "Email Order Intake" — wrong for an order
that arrived over EDI — because the labels lived only in the seeded DB and
never reached the UI. These labels are now projected from the governed
taxonomy (`db/seeds/case_taxonomy.yaml`) through the generated constants.

`intents_by_supergroup` drives the summary qualifier rule: the intent chip
is shown only when its supergroup fans out to more than one intent, so a
1:1 bucket like `SG_NEW_ORDER` reads as just "New Order".

Health takes no auth (mirrors test_health_autonomy.py).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def test_health_exposes_display_labels(client) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert "display_labels" in body
    labels = body["display_labels"]
    assert set(labels) >= {"supergroups", "intents"}
    assert isinstance(labels["supergroups"], dict) and labels["supergroups"]
    assert isinstance(labels["intents"], dict) and labels["intents"]


def test_manual_order_intake_label_is_not_the_email_misnomer(client) -> None:
    """The exact ADR-045 defect: the intent must read 'Manual Order Intake',
    never the channel-specific 'Email Order Intake' (the order can arrive
    via EDI/fax/phone). This is the contract-side guard against the UI
    misnomer regressing."""
    body = client.get("/api/v1/health").json()
    intents = body["display_labels"]["intents"]
    assert intents["INT_MANUAL_ORDER_INTAKE"] == "Manual Order Intake"
    assert "Email Order Intake" not in intents.values()


def test_supergroup_label_for_new_order(client) -> None:
    body = client.get("/api/v1/health").json()
    assert body["display_labels"]["supergroups"]["SG_NEW_ORDER"] == "New Order"


def test_intents_by_supergroup_drives_qualifier_rule(client) -> None:
    body = client.get("/api/v1/health").json()
    fanout = body["intents_by_supergroup"]
    assert isinstance(fanout, dict) and fanout
    # SG_NEW_ORDER is a 1:1 bucket — summary shows just "New Order", no
    # intent qualifier chip.
    assert fanout["SG_NEW_ORDER"] == ["INT_MANUAL_ORDER_INTAKE"]
    # SG_BLOCK_AVAILABILITY fans out — the intent IS the operator-meaningful
    # distinction (back-order vs over-max vs MOQ vs pallet), so the qualifier
    # must show. Guard the >1 invariant rather than the exact membership.
    assert len(fanout["SG_BLOCK_AVAILABILITY"]) > 1


def test_every_labelled_code_is_a_known_taxonomy_code(client) -> None:
    """Labels must not reference codes outside the governed taxonomy —
    that would be a partial-truth label the UI could render for a code the
    backend never emits."""
    from contracts._generated.taxonomy_constants import (
        INTENT_CODES,
        SUPERGROUP_CODES,
    )

    body = client.get("/api/v1/health").json()
    labels = body["display_labels"]
    assert set(labels["supergroups"]) <= set(SUPERGROUP_CODES)
    assert set(labels["intents"]) <= set(INTENT_CODES)
