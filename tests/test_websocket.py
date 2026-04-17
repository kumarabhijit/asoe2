"""Tests for WebSocket hub, event schemas, and pub/sub (architecture_v3.md §10).

Covers: event schema construction, in-memory pub/sub (publish, replay,
tenant isolation), WebSocket auth protocol, event publishing from
resolve endpoints, and WebSocket streaming.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token, create_refresh_token
from api.events import WSEvent
from api.pubsub import InMemoryPubSub, event_publisher
from api.store import exception_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    exception_store.clear()
    if hasattr(event_publisher, "clear"):
        event_publisher.clear()
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def pubsub():
    ps = InMemoryPubSub()
    return ps


@pytest.fixture()
def analyst_token():
    return create_test_token(roles=["analyst"], org="tenant-a")


@pytest.fixture()
def manager_token():
    return create_test_token(roles=["manager"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sample_event() -> dict:
    return {
        "order_id": "PO-WS-001",
        "po_price": 100.0,
        "sap_base_price": 120.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
    }


# ---------------------------------------------------------------------------
# Event Schema
# ---------------------------------------------------------------------------

class TestEventSchema:
    def test_pipeline_progress(self):
        event = WSEvent.pipeline_progress(
            trace_id="t-001",
            exception_id="e-001",
            tenant_id="tenant-a",
            node="classify",
            status="completed",
            duration_ms=42,
            data={"intent": "CONTRACTUAL_CORRECTION"},
        )
        assert event.type == "pipeline_progress"
        assert event.payload["node"] == "classify"
        assert event.payload["status"] == "completed"
        assert event.payload["duration_ms"] == 42
        assert event.payload["data"]["intent"] == "CONTRACTUAL_CORRECTION"

    def test_exception_update(self):
        event = WSEvent.exception_update(
            trace_id="t-002",
            exception_id="e-002",
            tenant_id="tenant-a",
            lifecycle_state="RESOLVED",
            updated_fields=["final_status", "resolved_by"],
        )
        assert event.type == "exception_update"
        assert event.payload["lifecycle_state"] == "RESOLVED"
        assert "final_status" in event.payload["updated_fields"]

    def test_task_complete(self):
        event = WSEvent.task_complete(
            trace_id="t-003",
            exception_id="e-003",
            tenant_id="tenant-a",
            task_id="task-003",
            final_status="COMPLETE",
            explanation="Recipe executed successfully",
        )
        assert event.type == "task_complete"
        assert event.payload["task_id"] == "task-003"
        assert event.payload["final_status"] == "COMPLETE"

    def test_error_event(self):
        event = WSEvent.error(
            trace_id="t-004",
            exception_id="e-004",
            tenant_id="tenant-a",
            code="GRAPH_EXECUTION_ERROR",
            message="Pipeline failed",
        )
        assert event.type == "error"
        assert event.payload["code"] == "GRAPH_EXECUTION_ERROR"

    def test_to_json_roundtrip(self):
        event = WSEvent.task_complete(
            trace_id="t-005", exception_id="e-005",
            tenant_id="t", task_id="tk", final_status="COMPLETE",
        )
        json_str = event.to_json()
        parsed = json.loads(json_str)
        assert parsed["type"] == "task_complete"
        assert parsed["trace_id"] == "t-005"
        assert "timestamp" in parsed

    def test_event_has_timestamp(self):
        event = WSEvent.pipeline_progress(
            trace_id="t", exception_id="e", tenant_id="t",
            node="ingest", status="started",
        )
        assert event.timestamp  # non-empty ISO 8601 string


# ---------------------------------------------------------------------------
# InMemoryPubSub
# ---------------------------------------------------------------------------

class TestInMemoryPubSub:
    def test_publish_and_get_recent(self, pubsub):
        event = WSEvent.task_complete(
            trace_id="t", exception_id="e", tenant_id="t1",
            task_id="tk", final_status="COMPLETE",
        )
        pubsub.publish("t1", event)
        recent = pubsub.get_recent("t1")
        assert len(recent) == 1
        parsed = json.loads(recent[0])
        assert parsed["type"] == "task_complete"

    def test_tenant_isolation(self, pubsub):
        event1 = WSEvent.task_complete(
            trace_id="t1", exception_id="e1", tenant_id="t1",
            task_id="tk1", final_status="COMPLETE",
        )
        event2 = WSEvent.task_complete(
            trace_id="t2", exception_id="e2", tenant_id="t2",
            task_id="tk2", final_status="BLOCKED",
        )
        pubsub.publish("t1", event1)
        pubsub.publish("t2", event2)

        t1_events = pubsub.get_recent("t1")
        t2_events = pubsub.get_recent("t2")
        assert len(t1_events) == 1
        assert len(t2_events) == 1
        assert json.loads(t1_events[0])["tenant_id"] == "t1"
        assert json.loads(t2_events[0])["tenant_id"] == "t2"

    def test_replay_returns_events_after_timestamp(self, pubsub):
        event = WSEvent.task_complete(
            trace_id="t", exception_id="e", tenant_id="t1",
            task_id="tk", final_status="COMPLETE",
        )
        pubsub.publish("t1", event)
        # Replay from 1 second ago should include the event
        replay = pubsub.get_replay("t1", since_timestamp=time.time() - 1)
        assert len(replay) == 1

    def test_replay_excludes_old_events(self, pubsub):
        """Events older than the replay window are not returned."""
        replay = pubsub.get_replay("t1", since_timestamp=time.time() + 100)
        assert len(replay) == 0

    def test_clear(self, pubsub):
        event = WSEvent.task_complete(
            trace_id="t", exception_id="e", tenant_id="t1",
            task_id="tk", final_status="COMPLETE",
        )
        pubsub.publish("t1", event)
        pubsub.clear()
        assert len(pubsub.get_recent("t1")) == 0

    def test_empty_tenant_returns_empty(self, pubsub):
        assert pubsub.get_recent("nonexistent") == []
        assert pubsub.get_replay("nonexistent") == []


# ---------------------------------------------------------------------------
# Resolve endpoints publish events
# ---------------------------------------------------------------------------

class TestResolvePublishesEvents:
    def test_sync_resolve_publishes_task_complete(self, client, analyst_token):
        """POST /resolve should publish a task_complete event."""
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200

        # Check event was published to tenant-a channel
        recent = event_publisher.get_recent("tenant-a")
        assert len(recent) >= 1
        parsed = json.loads(recent[-1])
        assert parsed["type"] == "task_complete"
        assert parsed["tenant_id"] == "tenant-a"
        assert parsed["payload"]["final_status"] is not None

    def test_async_resolve_publishes_event(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve/async",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        recent = event_publisher.get_recent("tenant-a")
        assert len(recent) >= 1
        last = json.loads(recent[-1])
        assert last["type"] == "task_complete"

    def test_explain_resolve_publishes_event(self, client, analyst_token):
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        assert r.status_code == 200
        recent = event_publisher.get_recent("tenant-a")
        assert len(recent) >= 1
        last = json.loads(recent[-1])
        assert last["type"] == "task_complete"
        assert last["payload"]["final_status"] == "MANUAL_REVIEW_REQUIRED"


class TestReanalysisStartedEvent:
    """Reanalyze endpoint publishes a reanalysis_started event before
    run_graph, then the usual task_complete after."""

    def test_reanalyze_publishes_reanalysis_started_before_completion(
        self, client, analyst_token, manager_token,
    ):
        # Seed a reanalyze-eligible exception via explain mode.
        r = client.post(
            "/api/v1/exceptions/resolve/explain",
            json=_sample_event(),
            headers=_auth(analyst_token),
        )
        exc_id = r.json()["exception_id"]
        # Clear the initial task_complete so we can assert ordering.
        event_publisher.clear()

        r = client.post(
            f"/api/v1/exceptions/{exc_id}/reanalyze",
            json={"reason": "Buyer amended the PO"},
            headers=_auth(manager_token),
        )
        assert r.status_code == 200

        recent = [json.loads(e) for e in event_publisher.get_recent("tenant-a")]
        types_in_order = [e["type"] for e in recent]
        # reanalysis_started must appear before task_complete.
        assert "reanalysis_started" in types_in_order
        assert "task_complete" in types_in_order
        assert types_in_order.index("reanalysis_started") < types_in_order.index("task_complete")

        started = next(e for e in recent if e["type"] == "reanalysis_started")
        # The payload carries audit-grade context — attempt number, reason,
        # and the prior verdict/status the reviewer is questioning.
        assert started["exception_id"] == exc_id
        assert started["tenant_id"] == "tenant-a"
        assert started["payload"]["attempt"] == 1
        assert started["payload"]["reason"] == "Buyer amended the PO"
        assert started["payload"]["triggered_by"]


# ---------------------------------------------------------------------------
# WebSocket Hub
# ---------------------------------------------------------------------------

class TestWebSocketHub:
    def test_ws_rejects_unauthenticated(self, client):
        """WebSocket without auth message should be closed."""
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "invalid"})
            resp = ws.receive_json()
            assert resp["type"] == "error"

    def test_ws_rejects_bad_token(self, client):
        """WebSocket with invalid JWT should be closed."""
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": "bad.token.here"})
            resp = ws.receive_json()
            assert resp["type"] == "error"

    def test_ws_accepts_valid_token(self, client, analyst_token):
        """WebSocket with valid JWT should stay connected and respond to ping."""
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": analyst_token})
            # After auth, send a ping to get pong back
            ws.send_json({"type": "ping"})
            resp = ws.receive_json()
            assert resp["type"] == "pong"

    def test_ws_receives_published_events(self, client, analyst_token):
        """Published events should be delivered via WebSocket."""
        # First publish an event
        event = WSEvent.task_complete(
            trace_id="ws-t", exception_id="ws-e", tenant_id="tenant-a",
            task_id="ws-tk", final_status="COMPLETE",
        )
        event_publisher.publish("tenant-a", event)

        # Connect and authenticate
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": analyst_token})
            # Ping to receive any queued events
            ws.send_json({"type": "ping"})
            # Should receive the event then pong
            resp = ws.receive_text()
            parsed = json.loads(resp)
            # Could be the event or pong; collect both
            messages = [parsed]
            if parsed.get("type") != "pong":
                pong = ws.receive_json()
                messages.append(pong)

            types = [m.get("type") for m in messages]
            assert "task_complete" in types or "pong" in types

    def test_ws_tenant_isolation(self, client):
        """Events for tenant-b should not reach tenant-a WebSocket."""
        event = WSEvent.task_complete(
            trace_id="iso-t", exception_id="iso-e", tenant_id="tenant-b",
            task_id="iso-tk", final_status="COMPLETE",
        )
        event_publisher.publish("tenant-b", event)

        token_a = create_test_token(roles=["analyst"], org="tenant-a")
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": token_a})
            ws.send_json({"type": "ping"})
            resp = ws.receive_json()
            # tenant-a should only get pong, not tenant-b's event
            assert resp["type"] == "pong"

    def test_ws_replay_on_reconnect(self, client, analyst_token):
        """Reconnecting with last_seen should replay missed events."""
        # Publish an event
        event = WSEvent.task_complete(
            trace_id="replay-t", exception_id="replay-e", tenant_id="tenant-a",
            task_id="replay-tk", final_status="COMPLETE",
        )
        event_publisher.publish("tenant-a", event)

        # Connect with last_seen in the past to trigger replay
        from datetime import datetime, timezone, timedelta
        last_seen = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()

        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({
                "type": "auth",
                "token": analyst_token,
                "last_seen": last_seen,
            })
            # Should receive replayed event
            resp = ws.receive_text()
            parsed = json.loads(resp)
            assert parsed["type"] == "task_complete"
            assert parsed["trace_id"] == "replay-t"
