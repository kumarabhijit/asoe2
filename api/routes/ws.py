"""WebSocket hub (architecture_v3.md Section 10.3).

ws://host/api/v1/ws — authenticated, tenant-scoped event streaming.

Protocol:
  1. Client connects to ws://host/api/v1/ws
  2. Client sends auth message: { "type": "auth", "token": "eyJ..." }
  3. Server validates JWT, extracts tenant_id, subscribes to Redis channel
  4. Server streams events for the tenant until disconnect
  5. On reconnect, client sends { "type": "auth", "token": "...", "last_seen": "ISO8601" }
     and receives replayed events from the 60-second buffer

Tenant isolation: each client receives events only for their tenant_id channel.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.deps import _get_jwt_secret, _jwt_decode, _expand_permissions
from api.pubsub import event_publisher, InMemoryPubSub

logger = logging.getLogger("asoe.api.ws")

router = APIRouter()


async def _authenticate_ws(websocket: WebSocket) -> Optional[dict]:
    """Wait for auth message and validate JWT. Returns claims or None."""
    try:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("WebSocket auth: invalid message: %s", exc)
        return None

    if msg.get("type") != "auth" or not msg.get("token"):
        return None

    try:
        payload = _jwt_decode(msg["token"], _get_jwt_secret())
    except (ValueError, KeyError) as exc:
        logger.warning("WebSocket auth: JWT validation failed: %s", exc)
        return None

    if not payload.get("org"):
        return None

    return {
        "tenant_id": payload["org"],
        "sub": payload.get("sub", ""),
        "roles": payload.get("roles", []),
        "last_seen": msg.get("last_seen"),
    }


@router.websocket("/ws")
async def websocket_hub(websocket: WebSocket) -> None:
    """WebSocket endpoint — authenticates, replays missed events, streams live."""
    await websocket.accept()

    # Step 1: Authenticate
    claims = await _authenticate_ws(websocket)
    if not claims:
        await websocket.send_json({"type": "error", "message": "Authentication failed."})
        await websocket.close(code=4001, reason="Authentication failed")
        return

    tenant_id = claims["tenant_id"]
    logger.info("WebSocket connected: tenant=%s sub=%s", tenant_id, claims["sub"])

    # Step 2: Replay missed events (if client provides last_seen timestamp)
    last_seen = claims.get("last_seen")
    if last_seen:
        try:
            since = datetime.fromisoformat(last_seen).timestamp()
        except (ValueError, TypeError):
            since = None
        if since:
            replay_events = event_publisher.get_replay(tenant_id, since_timestamp=since)
            for event_json in replay_events:
                await websocket.send_text(event_json)

    # Step 3: Stream live events
    try:
        if isinstance(event_publisher, InMemoryPubSub):
            # In-memory mode: poll for new events (no true pub/sub)
            await _stream_in_memory(websocket, tenant_id)
        else:
            # Redis mode: subscribe to pub/sub channel
            await _stream_redis(websocket, tenant_id)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: tenant=%s", tenant_id)
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)


async def _stream_in_memory(websocket: WebSocket, tenant_id: str) -> None:
    """In-memory streaming: client sends pings, server responds with new events."""
    seen_count = 0
    while True:
        # Wait for client message (ping or any message to keep alive)
        raw = await websocket.receive_text()
        msg = json.loads(raw) if raw else {}

        if msg.get("type") == "ping":
            # Return any new events since last check
            all_events = event_publisher.get_recent(tenant_id)
            new_events = all_events[seen_count:]
            for event_json in new_events:
                await websocket.send_text(event_json)
            seen_count = len(all_events)
            await websocket.send_json({"type": "pong"})


async def _stream_redis(websocket: WebSocket, tenant_id: str) -> None:
    """Redis streaming: subscribe to pub/sub channel, forward events."""
    import asyncio
    ps = event_publisher.subscribe(tenant_id)
    try:
        while True:
            # Check for messages from Redis (non-blocking with timeout)
            message = await asyncio.get_event_loop().run_in_executor(
                None, lambda: ps.get_message(timeout=1.0)
            )
            if message and message["type"] == "message":
                await websocket.send_text(message["data"])
    finally:
        ps.unsubscribe()
        ps.close()
