# Phase 13 — Database Layer (PostgreSQL Schema & Migrations)

```text
Read architecture_v3.md §9 (Data Architecture), §9.1 (Lifecycle), §9.2 (Schema),
§11.3 (Multi-Tenancy RLS), CLAUDE.md, DESIGN.md §16, and tasks.md (Phase 13).
Implement only Phase 13.

Requirements:

1. PostgreSQL migration SQL (db/migrations/V001__initial_schema.sql):
   - 5 tables per architecture_v3.md §9.2:
     - exceptions (UUID PK, tenant_id, order_id, intent CHECK constraint, lifecycle_state,
       resolution_data JSONB, trace_id, context_embedding VECTOR(1536))
     - traces (exception_id FK, trace_record JSONB)
     - policy_overrides (tenant_id + policy_key + effective_from UNIQUE)
     - policy_audit_log (immutability trigger: prevent UPDATE/DELETE — SOX requirement)
     - checkpoints (V1.1 HITL pause/resume — graph_state JSONB)
   - indexes: tenant+state, trace_id, tenant+order, audit tenant, pending checkpoints
   - Row-Level Security on exceptions, traces, policy_overrides, checkpoints per §11.3
   - RLS misconfiguration guard: current_setting('app.current_tenant_id', true) IS NOT NULL
   - schema_migrations version tracking table
   - Extensions: pgcrypto (gen_random_uuid), vector (pgvector V2 readiness)

2. Migration runner (db/migrations/runner.py):
   - auto-detect PostgreSQL vs SQLite from DATABASE_URL
   - SQLite-compatible subset schema for CI testing (no extensions, RLS, triggers, VECTOR)
   - idempotent execution (track applied versions in schema_migrations)
   - CLI entrypoint: DATABASE_URL=... python -m db.migrations.runner

3. Connection adapters (db/connection.py):
   - SQLiteAdapter: thread-local connections, WAL mode, foreign keys ON
   - PostgresAdapter: per-request connections, sets app.current_tenant_id session var for RLS
   - create_adapter(database_url) factory — auto-detect from URL scheme
   - Default (no DATABASE_URL) → SQLiteAdapter with in-memory database

4. Repository layer (db/repository.py):
   - ExceptionRepository: create, get, list (paginated + filtered by status/intent), update, stats
   - TraceRepository: create, get_by_exception
   - PolicyRepository: create_override (auto-creates audit log entry with previous_value),
     get_override (latest by effective_from), list_audit_log
   - All queries include tenant_id predicate (application-layer isolation)
   - JSON serialization/deserialization for JSONB fields

5. API integration (api/store.py):
   - DatabaseBackedStore: same interface as ExceptionStore (create, get, list, update,
     store_trace, get_trace, stats, clear)
   - Module-level singleton: DATABASE_URL set → DatabaseBackedStore, unset → ExceptionStore
   - API routes require zero changes

6. Docker Compose (docker-compose.yml):
   - Add postgres service (pgvector/pgvector:pg16) with healthcheck
   - Add redis service (redis:7-alpine) with healthcheck
   - Core service depends_on postgres + redis health
   - DATABASE_URL and REDIS_URL in shared x-core-env block
   - pgdata and redisdata volumes

Constraints:
- V1 uses raw SQL with Repository pattern (see ADR-002 for rationale)
- SQLite for CI testing — no PostgreSQL required in CI
- recipes must never import from db/
- do not add SQLAlchemy or ORM dependencies (see ADR-002 migration triggers)
- do not add speculative features beyond architecture_v3.md §9

Add tests for: schema creation (all 5 tables + schema_migrations), idempotent migration,
exception CRUD (create, get, get wrong tenant → None, lifecycle state mapping),
list (basic, filter by status, filter by intent, tenant isolation, pagination),
update (success + wrong tenant), stats, resolution_data JSON round-trip,
intent CHECK constraint, trace CRUD + tenant isolation, policy override with audit log
(previous_value tracking, latest override, tenant isolation), DatabaseBackedStore
round-trip (create, list, trace, update, stats, clear, tenant isolation).

Update: DESIGN.md (add §16 Database Layer), tasks.md (Phase 13 checklist),
pyproject.toml (add db package, postgres optional dependency group),
.env.example (DATABASE_URL, REDIS_URL, POSTGRES_* vars).
```
