# ADR-002: Database Access — Raw SQL Repository vs. ORM

**Status:** Accepted
**Date:** 2026-04-09
**Deciders:** Principal AI Systems Architect
**Applies to:** `db/` module (connection, repository, migrations)

---

## Context

The database layer (`db/`) provides CRUD operations for 3 entity groups — exceptions, traces, and policy overrides — against PostgreSQL (production) and SQLite (testing). The question is which data access pattern to use:

- **Option A: Raw SQL with Repository pattern** — hand-written SQL in repository classes, parameterized queries, no ORM dependency.
- **Option B: SQLAlchemy Core** — query builder + connection pooling, no object mapping. SQL is composable Python expressions.
- **Option C: SQLAlchemy ORM** — full object-relational mapping with sessions, unit-of-work, identity map.
- **Option D: SQLModel** — Pydantic + SQLAlchemy hybrid (created by the FastAPI author) that unifies API schemas and database models.

---

## Decision

**V1: Option A (raw SQL with Repository pattern).** The `db/repository.py` module uses hand-written parameterized SQL inside `ExceptionRepository`, `TraceRepository`, and `PolicyRepository`. The `db/connection.py` module provides `SQLiteAdapter` and `PostgresAdapter` with context-managed cursors.

The staged evolution path is:

| Stage | Trigger | Model | Change Required |
|---|---|---|---|
| **V1 (current)** | 3 entities, 5 methods each, single-table queries | Raw SQL + Repository (Option A) | None |
| **V1.5** | Dialect divergence causes bugs, OR connection pooling needed | SQLAlchemy Core (Option B) | Replace raw SQL strings with `sqlalchemy.text()` or expression builder. Add `create_engine()` with pool. Repository interface unchanged. |
| **V2** | > 8 entities, complex joins, polymorphic event model | SQLAlchemy Core + mapped models (Option B+) | Add declarative models alongside Pydantic contracts. Keep Pydantic for API boundaries, SQLAlchemy for persistence. |
| **V2 (alt)** | Team prefers unified Pydantic/DB models | SQLModel (Option D) | Replace Pydantic API schemas + raw SQL with SQLModel classes. Larger refactor but reduces model duplication. |

---

## Rationale

### Why raw SQL is appropriate at V1 scale

| Factor | Raw SQL (A) | Core (B) | ORM (C) | Assessment |
|---|---|---|---|---|
| **Lines of data access code** | ~300 | ~250 | ~200 + model definitions | Marginal difference at 3 entities |
| **Dependencies added** | 0 | `sqlalchemy` (~3 MB) | `sqlalchemy` | Raw SQL wins; stdlib `sqlite3` suffices for testing |
| **Readability** | SQL is visible and auditable | SQL hidden behind expression builder | SQL hidden behind session magic | Raw SQL wins for compliance audit (SOX requirement: auditors can read the queries) |
| **PostgreSQL-specific features** | Native (RLS, JSONB, triggers, pgvector) | Supported but verbose | Supported but requires workarounds | Raw SQL wins; RLS `SET` commands and `JSONB` operators are cleaner in raw SQL |
| **Connection pooling** | Not included (per-request connections) | Built-in (`create_engine(pool_size=...)`) | Built-in | **SQLAlchemy wins** — this is the strongest argument for Option B |
| **Dialect abstraction** | Manual (`?` vs `%s` placeholder divergence) | Automatic | Automatic | **SQLAlchemy wins** — this is a concrete risk in Option A |
| **Query composition** | String concatenation (bounded by simple queries) | Type-safe expression builder | Same | SQLAlchemy wins at scale; irrelevant for single-table CRUD |
| **Test setup** | `sqlite3` in-memory, zero deps | `create_engine("sqlite://")`, minimal deps | Same + session/transaction management | Raw SQL wins for CI simplicity |

### Known risks in Option A (and mitigations)

**1. Dialect divergence (`?` vs `%s` placeholders)**

SQLite uses `?` for parameter placeholders. PostgreSQL (`psycopg2`) uses `%s`. The current repository code uses `?` everywhere, which works for SQLite tests but will fail against PostgreSQL.

*Mitigation:* The `PostgresAdapter` must translate `?` → `%s` at the cursor boundary, OR the repository must use a dialect-aware placeholder. This is the most likely trigger for migrating to SQLAlchemy Core — if the translation becomes error-prone, `sqlalchemy.text()` with `:param` named bindings eliminates the problem entirely.

*Interim fix:* Add a `_param()` helper to the adapter that returns the correct placeholder style, or use named parameters (`:name` style) with adapter-level translation. This is a low-cost fix that defers the SQLAlchemy dependency.

**2. No connection pooling**

The `PostgresAdapter` creates a new connection per `cursor()` call. Under load (500 concurrent API clients per architecture_v3.md §2), this will exhaust PostgreSQL's `max_connections` (default 100).

*Mitigation:* Production deployment should use PgBouncer as a sidecar or add `psycopg_pool.ConnectionPool` to the adapter. SQLAlchemy's built-in pool is the alternative if Option B is adopted. This is independent of the ORM decision — both raw SQL and SQLAlchemy can use PgBouncer.

**3. Manual `_row_to_dict()` mapping**

Each repository has a `_row_to_dict()` method that manually maps database columns to dictionary keys. At 3 entities this is 30 lines of obvious code. At 10+ entities it becomes a maintenance burden.

*Mitigation:* SQLite's `sqlite3.Row` and psycopg2's `RealDictCursor` return dict-like objects. Switching cursor factories eliminates most manual mapping. If entity count grows beyond 8, declarative models (Option B+ or D) are justified.

### Why SQLAlchemy ORM (Option C) is not appropriate

1. **Pydantic already owns the type system.** `GraphState`, `OrderEvent`, `ExecutionLog`, `TraceRecord` are all Pydantic models. Adding SQLAlchemy mapped classes creates a parallel type system that must be kept in sync — exactly the duplication that CLAUDE.md warns against.

2. **Session/unit-of-work complexity is not needed.** The repository methods are single-statement CRUD operations with explicit `commit()`. There are no multi-entity transactional workflows that benefit from SQLAlchemy's session management. (The one exception — `PolicyRepository.create_override()` which inserts into two tables — is handled with a single cursor in a single transaction.)

3. **Identity map conflicts with the stateless architecture.** SQLAlchemy's identity map tracks objects across a session. ASOE's pipeline is stateless per `run_graph()` call. The identity map would be empty at the start of every request and flushed at the end — pure overhead.

### Why SQLModel (Option D) is deferred, not rejected

SQLModel unifies Pydantic and SQLAlchemy models, which would eliminate the `_row_to_dict()` mapping and the duplicate schema definitions. It's the right choice IF:
- The team adopts FastAPI's session-per-request pattern (currently not used)
- The API response schemas and database columns converge (currently they diverge — API schemas omit `context_embedding`, include computed fields)
- SQLModel matures beyond its current 0.x version

---

## Consequences

### Positive

- Zero additional dependencies in V1. Tests use stdlib `sqlite3` only.
- SQL is auditable — SOX auditors can read `V001__initial_schema.sql` and `repository.py` without understanding an ORM.
- Repository interface is narrow (create/get/list/update/stats) — any backend can implement it.
- Migration to SQLAlchemy Core requires changing only the SQL strings inside repository methods; the API routes and tests are unaffected.

### Negative

- `?` vs `%s` placeholder divergence requires attention when testing against PostgreSQL.
- No connection pooling out of the box — production needs PgBouncer or an explicit pool.
- Manual `_row_to_dict()` mapping adds ~10 lines per entity.

### Neutral

- The Repository interface must be preserved as the abstraction boundary. Any migration to SQLAlchemy Core or SQLModel should change the implementation inside the repository, never the interface consumed by API routes.

---

## Migration Triggers

Re-evaluate this decision (→ Option B: SQLAlchemy Core) when **any** of the following occur:

| # | Trigger | Rationale |
|---|---|---|
| 1 | **Entity count exceeds 8 tables with CRUD** | Manual `_row_to_dict()` mapping becomes a maintenance burden; declarative models earn their keep |
| 2 | **Dialect placeholder bug reaches production** | The `?` vs `%s` divergence is the most likely concrete failure; SQLAlchemy's `text()` eliminates it |
| 3 | **Multi-table transactions beyond 2 tables** | Raw SQL transaction management becomes error-prone; SQLAlchemy's session/savepoint handling is justified |
| 4 | **Query composition requires dynamic WHERE clauses with > 3 optional filters** | String-based SQL concatenation becomes fragile; expression builder is justified |
| 5 | **Connection pooling needs exceed what PgBouncer provides** | SQLAlchemy's pool with health checks and pre-ping is more configurable than PgBouncer for complex scenarios |
| 6 | **A second developer unfamiliar with raw SQL joins the team** | ORM reduces onboarding friction for developers who think in objects, not SQL |

---

## Expert Perspectives Considered

| Expert | Position | Key Argument |
|---|---|---|
| **Mike Bayer** (SQLAlchemy creator) | Use Core, not ORM | Core gives dialect abstraction + pooling without the session overhead; ORM is overkill for CRUD-only repositories |
| **Simon Willison** (Datasette creator) | Raw SQL is fine at this scale | Protect the boundary (repository interface) and address placeholder divergence; don't add an ORM for 3 tables |
| **Martin Fowler** (enterprise patterns) | Document the migration trigger | The Repository pattern is correct; the question is when manual Data Mapper code exceeds its maintainability threshold |
| **Sebastian Ramirez** (FastAPI creator) | SQLModel for Pydantic/DB unification | SQLModel eliminates the dual-model problem, but only earns its keep when API schemas and DB columns converge |
| **Hynek Schlawack** (Python infrastructure) | No ORM; add connection pooling | Unnecessary abstraction layers hurt readability; the missing piece is a pool, not an ORM |

---

## Compliance

This decision is referenced in:
- `DESIGN.md` §16 (Database Layer)
- `architecture_v3.md` §9.2 (PostgreSQL Schema)
