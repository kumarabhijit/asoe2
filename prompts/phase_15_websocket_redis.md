# Phase 15 — WebSocket / Redis Real-Time Event Publishing

```text
Read architecture_v3.md §10 (Real-Time Event Publishing), §9.3 (Redis Usage),
CLAUDE.md, DESIGN.md, and tasks.md (Phase 15).
Implement only Phase 15.

Requirements:

1. Event schemas (api/events.py):
   - WSEvent envelope: type, trace_id, exception_id, tenant_id, timestamp, payload
   - Event types: pipeline_progress, exception_update, task_complete, error
   - PipelineProgressPayload: node, status (started/completed/failed), duration_ms, data
   - ExceptionUpdatePayload: lifecycle_state, updated_fields
   - TaskCompletePayload: task_id, final_status, explanation
   - ErrorPayload: code, message
   - Factory class methods: WSEvent.pipeline_progress(), .exception_update(), .task_complete(), .error()
   - to_json() for serialization

2. Redis Pub/Sub manager (api/pubsub.py):
   - InMemoryPubSub: publish, get_recent, get_replay (timestamp-based), clear — for testing
   - RedisPubSub: publish to asoe:ws:{tenant_id} channel, sorted set replay buffer (60s TTL),
     subscribe for WebSocket streaming
   - create_pubsub() factory: REDIS_URL set → Redis, unset → in-memory
   - Publish failures: log warning, do not block (§9.3 partial failure recovery)
   - Module-level event_publisher singleton

3. WebSocket hub (api/routes/ws.py):
   - ws://host/api/v1/ws endpoint
   - Auth protocol: first message must be { "type": "auth", "token": "eyJ..." }
   - Server validates JWT, extracts tenant_id from org claim
   - Replay: if client sends last_seen timestamp, replay events from buffer
   - In-memory mode: client sends { "type": "ping" }, server returns new events + pong
   - Redis mode: subscribe to pub/sub channel, forward events as they arrive
   - Tenant isolation: client receives events only for their tenant_id

4. Resolve endpoint wiring (api/routes/exceptions.py):
   - All three resolve endpoints (sync, async, explain) publish task_complete after graph execution
   - Event includes trace_id, exception_id, tenant_id, final_status, explanation

Constraints:
- no changes to the core engine (contracts/, orchestration/, recipes/, etc.)
- InMemoryPubSub is the default (no Redis required for CI)
- publish failures must never block graph execution or API responses
- do not add client-side reconnection logic (that lives in asoe-ui)
- do not add speculative features beyond architecture_v3.md §10

Add tests for: event schema construction (all 4 types + JSON roundtrip + timestamp),
in-memory pub/sub (publish, get_recent, replay, tenant isolation, clear),
resolve endpoints publish events (sync, async, explain), WebSocket auth
(reject unauthenticated, reject bad token, accept valid), WebSocket streaming
(receives published events, tenant isolation, replay on reconnect).

Update: DESIGN.md, tasks.md, README.md (env vars, directory structure),
prompts/full_project_sequence.md, pyproject.toml (redis dependency).
```
