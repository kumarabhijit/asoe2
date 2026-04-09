"""Redis Pub/Sub manager (architecture_v3.md Section 9.3, 10.1).

Publishes WSEvent messages to per-tenant Redis channels and provides
a subscriber interface for the WebSocket hub.

Channels: ``asoe:ws:{tenant_id}``

When Redis is unavailable, publish operations log a warning and return
without blocking. The WebSocket hub degrades gracefully — clients can
poll REST endpoints as a fallback.

A 60-second replay buffer per tenant (Redis stream) allows reconnecting
clients to catch up on missed events (§10.3).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock
from typing import Any, AsyncGenerator, Dict, List, Optional

from api.events import WSEvent

logger = logging.getLogger("asoe.pubsub")

_CHANNEL_PREFIX = "asoe:ws:"
_REPLAY_BUFFER_SECONDS = 60


def _channel_for_tenant(tenant_id: str) -> str:
    return f"{_CHANNEL_PREFIX}{tenant_id}"


# ---------------------------------------------------------------------------
# In-memory fallback (no Redis)
# ---------------------------------------------------------------------------

class InMemoryPubSub:
    """In-memory pub/sub for testing and when REDIS_URL is unset.

    Stores events in per-tenant lists with timestamps for replay support.
    Thread-safe.
    """

    def __init__(self) -> None:
        self._channels: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._lock = Lock()

    def publish(self, tenant_id: str, event: WSEvent) -> None:
        with self._lock:
            self._channels[tenant_id].append({
                "event": event.to_json(),
                "timestamp": time.time(),
            })
            # Trim events older than replay buffer
            cutoff = time.time() - _REPLAY_BUFFER_SECONDS
            self._channels[tenant_id] = [
                e for e in self._channels[tenant_id]
                if e["timestamp"] >= cutoff
            ]

    def get_replay(
        self, tenant_id: str, since_timestamp: Optional[float] = None,
    ) -> List[str]:
        """Return events after ``since_timestamp`` for replay."""
        since = since_timestamp or (time.time() - _REPLAY_BUFFER_SECONDS)
        with self._lock:
            return [
                e["event"]
                for e in self._channels.get(tenant_id, [])
                if e["timestamp"] >= since
            ]

    def get_recent(self, tenant_id: str, limit: int = 50) -> List[str]:
        """Return the most recent events for a tenant."""
        with self._lock:
            entries = self._channels.get(tenant_id, [])
            return [e["event"] for e in entries[-limit:]]

    def clear(self) -> None:
        with self._lock:
            self._channels.clear()


# ---------------------------------------------------------------------------
# Redis-backed pub/sub
# ---------------------------------------------------------------------------

class RedisPubSub:
    """Redis-backed pub/sub using Redis Pub/Sub channels.

    Publish to ``asoe:ws:{tenant_id}`` channel. Also stores events
    in a per-tenant sorted set (replay buffer) keyed by timestamp
    for 60-second replay on client reconnect.
    """

    def __init__(self, redis_url: str) -> None:
        import redis as redis_lib
        self._client = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._redis_lib = redis_lib

    def publish(self, tenant_id: str, event: WSEvent) -> None:
        channel = _channel_for_tenant(tenant_id)
        event_json = event.to_json()
        try:
            # Publish to pub/sub channel for live subscribers
            self._client.publish(channel, event_json)
            # Store in sorted set for replay (score = timestamp)
            replay_key = f"asoe:replay:{tenant_id}"
            now = time.time()
            self._client.zadd(replay_key, {event_json: now})
            # Trim events older than replay buffer
            cutoff = now - _REPLAY_BUFFER_SECONDS
            self._client.zremrangebyscore(replay_key, "-inf", cutoff)
            # Set TTL on the replay key
            self._client.expire(replay_key, _REPLAY_BUFFER_SECONDS + 10)
        except Exception as exc:
            logger.warning("Redis publish failed (tenant=%s): %s", tenant_id, exc)

    def get_replay(
        self, tenant_id: str, since_timestamp: Optional[float] = None,
    ) -> List[str]:
        since = since_timestamp or (time.time() - _REPLAY_BUFFER_SECONDS)
        replay_key = f"asoe:replay:{tenant_id}"
        try:
            return self._client.zrangebyscore(replay_key, since, "+inf")
        except Exception as exc:
            logger.warning("Redis replay fetch failed: %s", exc)
            return []

    def subscribe(self, tenant_id: str):
        """Return a Redis pubsub subscription for the tenant channel."""
        ps = self._client.pubsub()
        ps.subscribe(_channel_for_tenant(tenant_id))
        return ps

    def clear(self) -> None:
        """Clear all replay buffers (testing only)."""
        try:
            for key in self._client.scan_iter("asoe:replay:*"):
                self._client.delete(key)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_pubsub() -> InMemoryPubSub | RedisPubSub:
    """Create the pub/sub backend based on REDIS_URL.

    When REDIS_URL is set → RedisPubSub (production).
    When unset → InMemoryPubSub (testing/development).
    """
    redis_url = os.getenv("REDIS_URL", "")
    if redis_url:
        try:
            return RedisPubSub(redis_url)
        except Exception as exc:
            logger.warning("Redis connection failed, falling back to in-memory: %s", exc)
            return InMemoryPubSub()
    return InMemoryPubSub()


# Module-level singleton
event_publisher = create_pubsub()
