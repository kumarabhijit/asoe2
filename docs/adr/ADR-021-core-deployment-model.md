# ADR-021: ASOE Core Deployment Model — Library vs. Service

**Status:** Accepted
**Date:** 2026-04-09
**Deciders:** Principal AI Systems Architect
**Applies to:** `asoe2` (core engine), `asoe-ui` (frontend — boundary reference only)

---

## Context

ASOE Core is the deterministic Skill-Shadow-Recipe engine that classifies exceptions, audits them against compliance policy, and executes immutable Python recipes. It is consumed by two runtime entry points:

1. **FastAPI API Server** — synchronous `run_graph()` for REST requests and the WebSocket event hub.
2. **Async Worker (Celery / ARQ)** — long-running `run_graph()` for queued tasks and Event Hubs ingestion.

The question is how `asoe-core` should be packaged and deployed relative to these consumers:

- **Option A: In-process library** — API server and worker import `asoe-core` as a Python package from the same codebase.
- **Option B: Standalone service** — `asoe-core` is extracted into a gRPC/REST service; API server and worker call it over the network.
- **Option C: Versioned library** — `asoe-core` is published as an internal Python package; API server and worker pin to a version and import it in-process.

---

## Decision

**V1: Option A (in-process library, single repo).** ASOE Core, the FastAPI API server, and the async worker all live in the `asoe2` repository. The API server and worker import `asoe-core` directly as a Python package — no network boundary.

The staged evolution path is:

| Stage | Trigger | Model | Change Required |
|---|---|---|---|
| **V1 (current)** | < 2 tenants, < 1K exceptions/hour | In-process library (Option A) | None |
| **V1.5** | 2+ tenants, independent worker scaling needed | Separate Dockerfiles + dependency groups, same repo | Split `pyproject.toml` extras (`[api]`, `[worker]`, `[core]`). Separate Docker entrypoints already exist. |
| **V2** | 5+ tenants, independent release cadence needed | Versioned internal package (Option C) | Extract `core/` to internal PyPI package. API and worker pin to a version. Zero interface change — still in-process import. |
| **V3** | 10K+ exceptions/hour, multi-region, OR core needs independently-scaled GPU inference | Core as a service (Option B) | Wrap `run_graph()` in gRPC handler. Write `CoreServiceClient` implementing the same interface. The hexagonal gateway layer already enforces this boundary. |

Each stage is entered only when its trigger condition is met — not on a calendar schedule.

---

## Rationale

### Why the library model does NOT block parallel scaling

The primary concern — "can multiple asoe-core instances run in parallel?" — is a non-issue under the library model:

1. **`run_graph()` is stateless.** It takes an `OrderEvent`, runs the 11-node pipeline, returns a `GraphState`. No in-process state is held between calls. No singleton. No global mutex.

2. **Horizontal scaling is already the design.** Deploy N worker pods, each imports `asoe-core`, each runs `run_graph()` independently. Celery `worker_concurrency=4` means 4 concurrent pipelines per pod. 10 pods = 40 concurrent pipelines. The library import is not the bottleneck.

3. **The real shared-state bottleneck is the database.** Whether core is a library or a service, all instances write to the same PostgreSQL (`exceptions`, `traces`, `policy_overrides`) and the same Redis (pub/sub, circuit breaker state). Scaling constraints are database connection pooling (PgBouncer) and write contention — not the import model.

### Why Option A is the right V1 choice

| Factor | Library (A) | Service (B) | Assessment |
|---|---|---|---|
| **Latency per `run_graph()`** | 0 (in-process) | 1–5ms network + serialization (GraphState is 2–50KB JSON) | Library wins. For an 8-min SLA this is noise, but it's pure overhead with zero benefit at V1 scale. |
| **Operational complexity** | 1 repo, 1 CI pipeline, shared types | 3 repos, 3 CI pipelines, proto/schema package for shared types, distributed tracing across service boundaries | Library wins decisively at small team size. |
| **Testing** | Full graph runs in-process with `StubGateway`. 584 tests, no mocks for inter-service calls. | Must mock the core service OR run integration tests against it. Test setup complexity increases. | Library wins. The current test suite is a major asset. |
| **Deploy cadence** | API + worker + core deploy together | Independent deploys, version compatibility matrix | Service wins at scale; irrelevant at V1 with 1 team and 1 deploy pipeline. |
| **Independent scaling** | Same image, different replica counts per Deployment | Full independence | Service wins at scale; library model achieves the same via separate Dockerfiles with different entrypoints. |
| **Type safety** | Shared Pydantic models, compile-time guarantees | Proto/schema package needed, potential drift between service and client | Library wins. |

### Where the library model creates coupling (and mitigations)

| Coupling | Impact | Mitigation |
|---|---|---|
| **Deploy coupling** | Changing a recipe forces redeploying the API server (same image) | Separate Dockerfiles with different entrypoints from the same source. Already implemented: `Dockerfile.core` (API), `Dockerfile.worker` (worker). A recipe change only requires redeploying worker pods if the API image is built separately. |
| **Dependency bloat** | FastAPI deps in worker image, Celery deps in API image | Dependency groups in `pyproject.toml`: `pip install asoe-core[api]` vs `pip install asoe-core[worker]`. Planned for V1.5. |
| **Version lock-step** | Can't gradually roll out a new recipe to workers while API stays on old version | Acceptable at V1 scale (single team, single tenant). Solved by Option C at V2 (versioned package with pinning). |
| **Test blast radius** | Breaking change in `api/` fails CI for `worker/` | Path-filtered CI jobs (e.g., `api/**` changes only trigger API tests). Low priority — current blast radius is manageable. |

### Why premature extraction (Option B at V1) would hurt

1. **Network hop for every `run_graph()`** — pure overhead with no scaling benefit at < 1K exceptions/hour.
2. **Operational tax** — three services = three health checks, three deploy pipelines, distributed tracing across boundaries, version compatibility matrix. This coordination cost is borne by a small team with no proportional reliability gain.
3. **Contract drift risk** — `GraphState` Pydantic model must be shared via a proto/schema package (a fourth artifact to version). Today it's a single import.
4. **Test regression** — the 584-test suite runs the full graph in-process with `StubGateway`. Extracting core means either mocking the service boundary (losing integration confidence) or standing up a test instance of the core service (adding CI complexity).

### The extraction seam already exists

The hexagonal gateway layer and the `run_graph()` function signature are the extraction seam:

```python
# Current (library import)
from orchestration.graph import run_graph
result = run_graph(order_event)

# Future (service client — same interface)
from core_client import CoreServiceClient
client = CoreServiceClient(endpoint="asoe-core:50051")
result = client.run_graph(order_event)
```

The `run_graph()` signature — typed input (`OrderEvent`), typed output (`GraphState`) — becomes the gRPC service contract. No recipe, gateway, compliance shadow, or constraint chain code changes. The migration cost is bounded by the clean interface.

---

## Consequences

### Positive

- Maximum development velocity in V1: single repo, single CI, shared types, in-process testing.
- Zero network overhead per `run_graph()` — the 8-min resolution SLA has no unnecessary latency contribution.
- 584-test suite runs fully in-process with no service mocks or test infrastructure.
- The staged evolution path (A → C → B) never requires rewriting core logic — only changing how consumers invoke `run_graph()`.

### Negative

- API server and worker deploy together in V1 (mitigated by separate Dockerfiles).
- Cannot do independent canary rollouts of core changes to workers vs. API in V1 (acceptable at current scale).
- Dependency groups not yet split in `pyproject.toml` — both images carry unused dependencies (low-priority V1.5 task).

### Neutral

- The extraction seam (`run_graph()` interface + hexagonal gateway layer) must be preserved as an invariant. Any change that makes `run_graph()` depend on in-process state (e.g., a process-level cache, a singleton connection pool) would compromise the migration path.

---

## Compliance

This decision is referenced in:
- `architecture_v3.md` §4 (System Architecture & Technical Stack)
- `consol_arch.md` §3 (Platform Architecture Overview) in the `asoe-ui` repository

---

## Review Triggers

Re-evaluate this decision when any of the following occur:
- A second production tenant is onboarded (V1.5 trigger)
- Exception throughput exceeds 500/hour sustained for 7 days
- The team grows beyond 3 engineers working on `asoe2` concurrently
- A business requirement demands independent release cadence for core vs. API (e.g., regulated tenant requiring change-freeze windows)
- GPU-backed inference (Compliance Shadow on Llama 3.1 8B) needs to scale independently of CPU-bound workers
