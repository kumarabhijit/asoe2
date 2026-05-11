"""ADR-039 §6.3 X.2→X.3 ratification gate — reviewer-override-of-LLM-
downgrade counter.

These tests pin the wiring between the `/exceptions/{id}/disposition`
+ `/override/cosign` paths and the
`shadow_llm_metrics.reviewer_overrides_of_llm_downgrade_total`
counter. The counter only increments when:

  1. The trace persisted at `/resolve*` time carries
     ``llm_shadow_verdict_action == "DISAGREE_DOWNGRADE"`` AND
  2. The operator's disposition sub_type is `OVERRIDE`
     (action != recommended).

Both APPROVE and REJECT dispositions are *not* counted — those
aren't "operator disagrees with the L2 downgrade" signals. The
non-DOWNGRADE traces (AGREE, ABSTAIN, no LLM verdict at all) are
also not counted.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.store import exception_store
from compliance.shadow_llm import (
    shadow_llm_cache,
    shadow_llm_metrics,
)
from api.deps import create_test_token


@pytest.fixture(autouse=True)
def _reset():
    shadow_llm_metrics.reset()
    shadow_llm_cache.clear()
    yield
    shadow_llm_metrics.reset()
    shadow_llm_cache.clear()


@pytest.fixture()
def client():
    return TestClient(create_app(), raise_server_exceptions=True)


@pytest.fixture()
def manager_token():
    return create_test_token(
        sub="test-user", roles=["manager"], org="tenant-a",
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sample_event() -> dict:
    return {
        "order_id": "PO-RVOL-1",
        "po_price": 100.0,
        "sap_base_price": 120.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
    }


def _create_pending_review(client, token: str) -> str:
    r = client.post(
        "/api/v1/exceptions/resolve/explain",
        json=_sample_event(),
        headers=_auth(token),
    )
    assert r.status_code == 200
    return r.json()["exception_id"]


def _stamp_llm_downgrade_on_trace(exception_id: str) -> None:
    """Inject ``llm_shadow_verdict_action`` on the persisted trace so
    the override handler sees the L2 downgrade signal. Real traffic
    writes this from `_persist_exception`; sandbox-mode `/resolve/explain`
    doesn't invoke the L2 shadow, so we stamp the trace directly."""
    trace = exception_store.get_trace(exception_id) or {}
    trace["llm_shadow_verdict_action"] = "DISAGREE_DOWNGRADE"
    exception_store.store_trace(exception_id, trace)


# ---------------------------------------------------------------------------


class TestReviewerOverrideOfLLMDowngradeCounter:
    def test_override_on_downgraded_record_increments_counter(
        self, client, manager_token,
    ):
        exc_id = _create_pending_review(client, manager_token)
        _stamp_llm_downgrade_on_trace(exc_id)
        assert shadow_llm_metrics.reviewer_overrides_of_llm_downgrade_total == 0

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "reason_tag": "OTHER",
                "notes": "Verified with buyer — proceeding despite L2 downgrade",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text
        assert shadow_llm_metrics.reviewer_overrides_of_llm_downgrade_total == 1

    def test_approve_on_downgraded_record_does_not_count(
        self, client, manager_token,
    ):
        """APPROVE (recommended action) is not a 'reviewer overrode the
        L2 downgrade' signal — the operator AGREED with the recommendation."""
        exc_id = _create_pending_review(client, manager_token)
        _stamp_llm_downgrade_on_trace(exc_id)

        # Force a recommended_action onto the record so sub_type can
        # deterministically resolve to APPROVE. The deterministic-
        # fallback path for EDI_850_PRICE_MISMATCH doesn't always
        # populate recommended_action, which would otherwise fall
        # through to OVERRIDE and miss the assertion we care about.
        record = exception_store.get(exc_id, "tenant-a")
        merged = dict(record.resolution_data or {})
        merged["recommended_action"] = "ESCALATE"
        exception_store.update(exc_id, "tenant-a", resolution_data=merged)

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ESCALATE",
                "reason_tag": "OTHER",
                "notes": "Concurring with recommendation",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["resolved_action"] == "ESCALATE"
        # APPROVE → counter is unchanged.
        assert (
            shadow_llm_metrics.reviewer_overrides_of_llm_downgrade_total == 0
        )

    def test_override_without_downgrade_signal_does_not_count(
        self, client, manager_token,
    ):
        """No `llm_shadow_verdict_action` on the trace = no signal,
        override does not feed the X.2→X.3 gate counter."""
        exc_id = _create_pending_review(client, manager_token)
        # Deliberately do NOT stamp the trace.
        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "reason_tag": "OTHER",
                "notes": "Manager override on a non-L2 record",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text
        assert (
            shadow_llm_metrics.reviewer_overrides_of_llm_downgrade_total == 0
        )

    def test_override_with_agree_verdict_does_not_count(
        self, client, manager_token,
    ):
        """The counter is gated on DISAGREE_DOWNGRADE specifically —
        AGREE and ABSTAIN traces shouldn't increment it."""
        exc_id = _create_pending_review(client, manager_token)
        trace = exception_store.get_trace(exc_id) or {}
        trace["llm_shadow_verdict_action"] = "AGREE"
        exception_store.store_trace(exc_id, trace)

        r = client.patch(
            f"/api/v1/exceptions/{exc_id}/disposition",
            json={
                "action": "ALLOW_BOTH",
                "reason_tag": "OTHER",
                "notes": "Override on an AGREE verdict",
            },
            headers=_auth(manager_token),
        )
        assert r.status_code == 200, r.text
        assert (
            shadow_llm_metrics.reviewer_overrides_of_llm_downgrade_total == 0
        )
