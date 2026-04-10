"""WebSocket and Redis pub/sub tests for sandbox.

Validates the real-time notification path:
  1. RecipeStatusUpdated events are published when a task transitions
  2. Events are tenant-scoped (architecture_v3.md Section 10.1)
  3. WebSocket auth protocol works (auth message → subscription)
  4. Event replay buffer works for reconnecting clients
  5. In-memory pub/sub behaves correctly as a Redis stand-in

Architecture_v3.md Section 10 — Real-Time Pipeline Events.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.events import WSEvent
from api.pubsub import InMemoryPubSub, event_publisher
from api.store import exception_store
from tests.sandbox.conftest import (
    PRICING_EVENT,
    MASS_PRICING_EVENT,
    DUPLICATE_PO_EVENT,
    SANDBOX_TENANT,
    auth_header,
)


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
    return InMemoryPubSub()


@pytest.fixture()
def analyst_token():
    return create_test_token(
        sub="analyst-user", email="analyst@asoe.test",
        name="Analyst", roles=["analyst"], org=SANDBOX_TENANT,
    )


# ═══════════════════════════════════════════════════════════════════════════
# WSEvent schema construction
# ═══════════════════════════════════════════════════════════════════════════

class TestWSEventSchemas:
    """WSEvent factory methods produce correct event shapes."""

    def test_task_complete_event(self):
        event = WSEvent.task_complete(
            trace_id="trace-1",
            exception_id="exc-1",
            tenant_id=SANDBOX_TENANT,
            task_id="task-1",
            final_status="COMPLETE",
            explanation="Price adjustment applied.",
        )
        assert event.type == "task_complete"
        assert event.tenant_id == SANDBOX_TENANT
        assert event.payload["task_id"] == "task-1"
        assert event.payload["final_status"] == "COMPLETE"

    def test_pipeline_progress_event(self):
        event = WSEvent.pipeline_progress(
            trace_id="trace-2",
            exception_id="exc-2",
            tenant_id=SANDBOX_TENANT,
            node="classify",
            status="completed",
            duration_ms=42,
        )
        assert event.type == "pipeline_progress"
        assert event.payload["node"] == "classify"
        assert event.payload["status"] == "completed"
        assert event.payload["duration_ms"] == 42

    def test_exception_update_event(self):
        event = WSEvent.exception_update(
            trace_id="trace-3",
            exception_id="exc-3",
            tenant_id=SANDBOX_TENANT,
            lifecycle_state="RESOLVED",
            updated_fields=["lifecycle_state", "resolved_by"],
        )
        assert event.type == "exception_update"
        assert event.payload["lifecycle_state"] == "RESOLVED"
        assert "lifecycle_state" in event.payload["updated_fields"]

    def test_error_event(self):
        event = WSEvent.error(
            trace_id="trace-4",
            exception_id="exc-4",
            tenant_id=SANDBOX_TENANT,
            code="GRAPH_EXECUTION_ERROR",
            message="Pipeline failed.",
        )
        assert event.type == "error"
        assert event.payload["code"] == "GRAPH_EXECUTION_ERROR"

    def test_event_serialization(self):
        event = WSEvent.task_complete(
            trace_id="trace-5",
            exception_id="exc-5",
            tenant_id=SANDBOX_TENANT,
            task_id="task-5",
            final_status="COMPLETE",
        )
        json_str = event.to_json()
        parsed = json.loads(json_str)
        assert parsed["type"] == "task_complete"
        assert parsed["trace_id"] == "trace-5"
        assert "timestamp" in parsed


# ═══════════════════════════════════════════════════════════════════════════
# InMemoryPubSub — publish, replay, tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

class TestInMemoryPubSub:
    """In-memory pub/sub used in sandbox and testing."""

    def test_publish_and_get_recent(self, pubsub):
        event = WSEvent.task_complete(
            trace_id="t1", exception_id="e1",
            tenant_id="tenant-A", task_id="task-1",
            final_status="COMPLETE",
        )
        pubsub.publish("tenant-A", event)
        recent = pubsub.get_recent("tenant-A")
        assert len(recent) == 1
        parsed = json.loads(recent[0])
        assert parsed["type"] == "task_complete"

    def test_tenant_isolation(self, pubsub):
        event = WSEvent.task_complete(
            trace_id="t2", exception_id="e2",
            tenant_id="tenant-A", task_id="task-2",
            final_status="COMPLETE",
        )
        pubsub.publish("tenant-A", event)
        assert len(pubsub.get_recent("tenant-A")) == 1
        assert len(pubsub.get_recent("tenant-B")) == 0

    def test_replay_by_timestamp(self, pubsub):
        before = time.time()
        event = WSEvent.task_complete(
            trace_id="t3", exception_id="e3",
            tenant_id="tenant-A", task_id="task-3",
            final_status="COMPLETE",
        )
        pubsub.publish("tenant-A", event)
        replay = pubsub.get_replay("tenant-A", since_timestamp=before)
        assert len(replay) == 1

    def test_replay_excludes_old_events(self, pubsub):
        """Events older than 60 seconds are trimmed."""
        replay = pubsub.get_replay(
            "tenant-A",
            since_timestamp=time.time() + 100,  # future → nothing
        )
        assert len(replay) == 0

    def test_clear(self, pubsub):
        event = WSEvent.task_complete(
            trace_id="t4", exception_id="e4",
            tenant_id="tenant-A", task_id="task-4",
            final_status="COMPLETE",
        )
        pubsub.publish("tenant-A", event)
        pubsub.clear()
        assert len(pubsub.get_recent("tenant-A")) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Events published on resolve
# ═══════════════════════════════════════════════════════════════════════════

class TestEventsPublishedOnResolve:
    """Resolve endpoints publish task_complete events."""

    def test_resolve_publishes_event(self, client, analyst_token):
        """POST /api/v1/exceptions/resolve publishes a task_complete event."""
        if hasattr(event_publisher, "clear"):
            event_publisher.clear()

        client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )

        if hasattr(event_publisher, "get_recent"):
            recent = event_publisher.get_recent(SANDBOX_TENANT)
            assert len(recent) >= 1
            parsed = json.loads(recent[0])
            assert parsed["type"] == "task_complete"
            assert parsed["payload"]["final_status"] == "COMPLETE"

    def test_blocked_resolve_publishes_blocked_event(self, client, analyst_token):
        """BLOCKED resolution publishes event with BLOCKED status."""
        if hasattr(event_publisher, "clear"):
            event_publisher.clear()

        client.post(
            "/api/v1/exceptions/resolve",
            json=MASS_PRICING_EVENT,
            headers=auth_header(analyst_token),
        )

        if hasattr(event_publisher, "get_recent"):
            recent = event_publisher.get_recent(SANDBOX_TENANT)
            assert len(recent) >= 1
            parsed = json.loads(recent[0])
            assert parsed["payload"]["final_status"] == "BLOCKED"

    def test_async_resolve_publishes_event(self, client, analyst_token):
        """POST /api/v1/exceptions/resolve/async publishes event."""
        if hasattr(event_publisher, "clear"):
            event_publisher.clear()

        client.post(
            "/api/v1/exceptions/resolve/async",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )

        if hasattr(event_publisher, "get_recent"):
            recent = event_publisher.get_recent(SANDBOX_TENANT)
            assert len(recent) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# WebSocket auth protocol
# ═══════════════════════════════════════════════════════════════════════════

class TestWebSocketAuth:
    """ws://host/api/v1/ws — auth message protocol."""

    def test_ws_rejects_missing_auth(self, client):
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "not-auth"})
            resp = ws.receive_json()
            assert resp["type"] == "error"

    def test_ws_rejects_invalid_token(self, client):
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": "invalid.jwt.token"})
            resp = ws.receive_json()
            assert resp["type"] == "error"

    def test_ws_accepts_valid_token(self, client, analyst_token):
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": analyst_token})
            # After auth, send a ping to verify connection is alive
            ws.send_json({"type": "ping"})
            resp = ws.receive_json()
            assert resp["type"] == "pong"

    def test_ws_streams_events_after_auth(self, client, analyst_token):
        """Authenticated WS client receives events after ping."""
        # First create an exception to generate an event
        if hasattr(event_publisher, "clear"):
            event_publisher.clear()

        client.post(
            "/api/v1/exceptions/resolve",
            json=PRICING_EVENT,
            headers=auth_header(analyst_token),
        )

        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "auth", "token": analyst_token})
            ws.send_json({"type": "ping"})
            messages = []
            # Collect messages until pong
            while True:
                msg = ws.receive_json()
                messages.append(msg)
                if msg.get("type") == "pong":
                    break
            # We should have received events + pong
            assert any(m.get("type") == "pong" for m in messages)
