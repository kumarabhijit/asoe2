# ADR-030: 5-Level Config Override Hierarchy & Resolution Semantics

**Status:** Accepted (revisions per 2026-05-10 review)
**Date:** 2026-05-03 (initial); 2026-05-10 (revisions)
**Deciders:** Same as ADR-028 (review session 2026-05-03; sign-off review 2026-05-10).
**Applies to:** `gateways/tenant_config.py` (new), `db/migrations/V006__tenant_config.sql` (new), `api/routes/policies.py`, new `api/routes/config.py`, `contracts/models.py` (`ConfigChange` event with typed scope union), `contracts/policy.py` (platform defaults stay; per-tenant resolution moves to gateway), `tests/test_tenant_config_gateway.py`.
**Related:** ADR-028, ADR-029, ADR-032, ADR-033.

---

## Context

The duplicate-PO spec (and `docs/specs/duplicate-po/calibration-methodology.md`) defines a 5-level config override hierarchy as the channel through which customers' calibrated weights, thresholds, and tier-specific windows enter the system:

```
L1 Platform defaults
   └── L2 Tenant defaults
       └── L3 Customer-tier (Strategic / Standard / SMB)
           └── L4 Customer-specific (e.g., Walmart)
               └── L5 Customer-channel (e.g., Walmart-EDI vs Walmart-Portal)
```

Today, all detection thresholds and weights live as module-level constants in `contracts/policy.py`. There is exactly one platform default. There is no storage for per-tenant or per-customer overrides, no resolution function, and no audit of config changes.

Per ADR-032, calibration is deferred — but the *expectation is that customers/POs supply calibrated values via config*. Without the override hierarchy in V1, those calibrated values have nowhere to land, and the deferral premise breaks.

End-user representation in the design review made the operational requirements concrete:

- **Tenant Admin (U3):** owns this surface. Needs sandbox-vs-production separation, audited changes (who/when/before/after), and a health endpoint to verify a resolved config for a sample `(tenant, customer, channel)`.
- **CS Manager (U2):** wants per-customer autonomy-level adjustments (e.g., downgrade Walmart from L4 to L3 on the day a buyer dispute lands) without filing a ticket.
- **SOX (E5):** every config change is a SOX-relevant event; immutable audit on every write.
- **Multi-tenant lens (E4):** resolution must be deterministic and observable — given `(tenant, customer, tier, channel)`, return the same merged result and the per-layer trace.

---

## Decision

### A. Storage and resolver shape

Storage and resolution function are written for all 5 levels in V1. UI to populate L4 and L5 is V1.5; V1 admins set L4/L5 via API. This avoids re-architecture later while accepting a smaller V1 surface.

> **2026-05-10 review revision (E4):** V1 ships **single-replica only** for the API tier that hosts the `tenant_config` resolver cache. Multi-replica deployment requires cross-process cache invalidation (pub/sub), which is V2 scope. The single-replica V1 constraint is documented here so a future scaling effort doesn't silently break cache consistency.

### B. Schema (`db/migrations/V006__tenant_config.sql`)

```sql
CREATE TABLE tenant_config (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    intent          VARCHAR(40) NOT NULL,
    layer           VARCHAR(20) NOT NULL,
    customer_tier   VARCHAR(20),
    customer_id     UUID REFERENCES customers(id),
    channel         VARCHAR(20),
    config          JSONB NOT NULL,
    environment     VARCHAR(10) NOT NULL DEFAULT 'production',
    version         INT NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      UUID REFERENCES users(id),
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (tenant_id, intent, layer, customer_tier, customer_id, channel, environment, version)
);

CREATE INDEX idx_tenant_config_resolve
    ON tenant_config (tenant_id, intent, environment, layer)
    WHERE is_current = TRUE;
```

L1 platform defaults remain in `contracts/policy.py` (the single platform-wide source of truth) and are not stored in this table — they are the bottom of every resolution stack and need no per-tenant variation.

### C. Resolution function (binding semantics)

```
resolve(tenant_id, intent, customer_id, customer_tier, channel, environment="production")
    -> (resolved_config: dict, trace: list[LayerContribution])
```

Order of application (deterministic):

1. Start with L1 platform defaults from `contracts/policy.py` for the given intent.
2. If `tenant_id` has a TENANT-layer row for this intent + environment, layer it on top.
3. If `customer_tier` is non-null and a TIER-layer row exists for `(tenant, intent, tier, environment)`, layer it.
4. If `customer_id` is non-null and a CUSTOMER-layer row exists for `(tenant, intent, customer, environment)`, layer it.
5. If `channel` is non-null and a CHANNEL-layer row exists for `(tenant, intent, customer, channel, environment)`, layer it.

For weight maps specifically, the merge follows ADR-029 (key-by-key override; sum-to-1 validated within `1e-4` tolerance after full resolution; fail-closed to L1 on violation).

For non-weight scalars (thresholds, lookback windows, tolerance bands, autonomy levels), later layers override earlier layers wholesale at the key level. No partial scalar merging.

### D. Per-layer trace (observability)

Every resolution emits a `LayerContribution` list that records, for each final value, which layer produced it:

```json
[
  { "layer": "PLATFORM", "key": "score_weights.po_number", "value": 0.30 },
  { "layer": "TIER",     "key": "score_weights.po_number", "value": 0.25, "tier": "standard" },
  { "layer": "CUSTOMER", "key": "score_weights.po_number", "value": 0.10, "customer_id": "cust_walmart" },
  { "layer": "CHANNEL",  "key": "score_weights.po_number", "value": 0.10, "customer_id": "cust_walmart", "channel": "EDI" }
]
```

Final value for `score_weights.po_number` is the last entry: 0.10 from L5. Trace is included in the gateway response and surfaced in the read envelope (ADR-028 Guard-rail 2). Admins can answer "why does this customer have these weights?" without re-deriving the merge mentally.

### E. Sandbox vs production

`environment: 'production' | 'sandbox'`. Resolution always queries one or the other; no cross-environment leakage. Sandbox is the test surface for proposed changes (admin edits sandbox, runs validation against synthetic events, then promotes to production via an explicit copy operation).

Promote-to-production is an atomic operation:

```
POST /api/v1/config/promote
{ "tenant_id": "...", "intent": "DUPLICATE_PO", "scope": { ... }, "comment": "..." }
```

Promotion writes new production rows with `version = current_version + 1`, marks the previous `is_current = FALSE`, and emits a `ConfigChange` event (see G).

### F. API surface (V1)

```
GET    /api/v1/config/:intent/resolve?tenant_id=&customer_id=&customer_tier=&channel=&environment=
       -> { resolved: {...}, trace: [...] }

GET    /api/v1/config/:intent
       -> list of all override rows for the calling tenant

POST   /api/v1/config/:intent/:layer
       -> create or update an override row at a specific layer

DELETE /api/v1/config/:intent/:layer/:row_id
       -> soft-delete (is_current = FALSE; audit-chained)

POST   /api/v1/config/promote
       -> sandbox → production
```

V1 ships the API only. Admin UI for L4 and L5 is V1.5 (CS Manager (U2)'s autonomy-tuning request rides on this). V1 admins use API + curl/Postman; acceptable per U3 in design review.

### G. ConfigChange domain event (binding)

Every write to `tenant_config` (create, update, soft-delete, promote) emits a `ConfigChange` event modeled in `contracts/models.py`:

```python
class TenantScope(BaseModel):
    layer: Literal["TENANT"]

class TierScope(BaseModel):
    layer: Literal["TIER"]
    customer_tier: str

class CustomerScope(BaseModel):
    layer: Literal["CUSTOMER"]
    customer_id: UUID

class ChannelScope(BaseModel):
    layer: Literal["CHANNEL"]
    customer_id: UUID
    channel: str

ConfigScope = Annotated[
    TenantScope | TierScope | CustomerScope | ChannelScope,
    Field(discriminator="layer"),
]

class ConfigChange(BaseModel):
    id: UUID
    tenant_id: UUID
    intent: Intent
    scope: ConfigScope                # discriminated union, not arbitrary dict
    environment: ConfigEnvironment
    operation: ConfigOperation
    before: dict | None
    after: dict | None
    actor_id: UUID
    actor_role: str
    comment: str | None
    timestamp: datetime
    version: int
```

> **2026-05-10 review revision (E1):** `scope` was originally typed as `dict` — DDD lens objection that this is a generic event with a payload bag, not a typed domain event. Revised to a Pydantic discriminated union (`TenantScope | TierScope | CustomerScope | ChannelScope`). Same audit-chain serialization, more type safety, no runtime cost.

`ConfigChange` is treated as a first-class domain event (DDD lens requirement), not a row in a generic audit-log table. It is appended to `audit_hash_chain` per the existing ADR-023 mechanism; a config change is as auditable as a resolution decision.

> **2026-05-10 review revision (E3):** `before` and `after` are full JSONB payloads. For weight maps and threshold maps, payload sizes are typically **≤1KB per entry** — negligible at audit-chain scale. The chain is bounded for V1 use cases. If future config types grow large payloads (multi-MB), a separate ADR will introduce diff-only or pointer-based serialization. Documented here so future readers don't assume the chain stores something unbounded.

### H. Health endpoint

`GET /api/v1/health/config-resolution?tenant_id=&intent=&customer_id=&customer_tier=&channel=` returns the resolved config + trace for a sample tuple. Used by admins to verify their override landed correctly. Returns 200 + payload on success; never returns 5xx — invalid arguments produce a 400 with explanation.

Also exposed: `GET /api/v1/health.allowed_override_reason_tags_by_intent` (already wired in current `health.py`) — UI consumes this to narrow `OverrideChooserDialog` per ADR-033.

---

## Rationale

- **Calibration enablement:** The deferral in ADR-032 only works if customer-supplied calibrated values have a destination. The 5-level hierarchy is that destination.
- **Hierarchy honesty (DDD):** Each level is a distinct authority — platform engineers own L1, tenant admins own L2, customer-success owns L3 + L4, integration owns L5. Modeling them as one flat config-blob would conflate authorities. Discriminated-union scope typing makes the per-layer authority structurally visible.
- **Observability (multi-tenant SaaS):** Per-layer trace turns every weighted decision into an answerable "why."
- **SOX:** Every config write is a `ConfigChange` event in the audit chain. No silent edits, no "I'll fix it later" untracked drift.
- **Operational pragmatism:** Sandbox-vs-production with an atomic promote operation matches how admins actually work; lets them validate proposed changes against synthetic events before risking production traffic.
- **Scope discipline:** L4/L5 *storage and resolution* in V1, *UI* in V1.5. Keeps V1 deliverable while not painting future work into a corner.

---

## Phased rollout

### V1 (this ADR's scope)

1. Migration `V006__tenant_config.sql`.
2. `gateways/tenant_config.py::resolve_for_event` implementing the 5-level merge + trace; backed by `tenant_config` table for L2–L5, `contracts/policy.py` for L1. **Single-replica process-local cache (TTL-60s);** cache-bust on `POST /api/v1/config/promote` within the same process. Multi-replica is V2.
3. `contracts/models.py` — add `ConfigChange`, `ConfigScope` discriminated union (`TenantScope`, `TierScope`, `CustomerScope`, `ChannelScope`), `ConfigEnvironment`, `ConfigOperation`.
4. `api/routes/config.py` — implement the 5 endpoints in F.
5. `api/routes/health.py` — add `/health/config-resolution`.
6. Hash-chain integration: `ConfigChange` events append to `audit_hash_chain`.
7. CI gate: every test in `tests/test_tenant_config_gateway.py` covers create, read, resolve at each layer, promote, and the trace shape.

### V1.5

1. **Admin UI** (in `asoe-ui`) for L4 (customer-specific) and L5 (customer-channel) editing with sandbox-promote workflow. **Committed** scope, not "backlog" — per 2026-05-10 review revision tightening the commitment language.
2. **CS-Manager-facing autonomy-tuning UI** (write a customer-specific override that adjusts only `autonomy_levels`) — **committed V1.5**, not backlog.
3. `customer_behavior_overrides` (blanket_po, drop_ship, high_frequency from spec) as named presets the admin can apply to a customer at L4 — these materialize into normal CUSTOMER-layer rows; they are not a sixth layer.

### V2

- Multi-replica deployment + cross-process cache invalidation (pub/sub or equivalent).
- Bulk import / export of tenant configurations.
- Cross-tenant config templates (carefully — needs its own ADR for the cross-tenant authorization model).

---

## Consequences

### Positive

- Calibrated values have a home from day one of V1.
- Every config change is auditable with full before/after capture.
- Admins can diagnose "why this customer got these values" from the trace alone.
- Recipe stays pure; configuration concerns live in `tenant_config` gateway.
- `ConfigChange.scope` is a typed discriminated union, not a payload bag — DDD purity preserved at the event level.

### Negative

- Adds a new gateway with non-trivial query patterns. Needs caching by (tenant, intent, customer, channel) to keep per-event latency low; cache invalidation on `ConfigChange` is required.
- L4 and L5 in V1 require API-level admin work (curl/Postman). Acceptable per U3 in design review; V1.5 UI work is firmly committed.
- `ConfigChange` events grow the audit chain. Volume estimate at V1 scale: <100 changes/tenant/month — negligible. Payload size ≤1KB per entry — also negligible.
- Single-replica V1 deployment limits horizontal scale until V2 brings multi-replica + pub/sub cache invalidation.

### Compliance notes

- `ConfigChange` carries `actor_id` and `actor_role` from the authenticated request. SOX requirement satisfied.
- Sandbox configs cannot be referenced by production resolution paths (separate `environment` filter on the query). Eliminates the risk of "I tested in sandbox but production picked up the test config."
- Promote-to-production requires the same authorization as direct production writes. No back-door promotions.

---

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **3-level only in V1 (platform, tenant, customer)** | U3 (admin) accepted only if the data model and resolver were written for 5 from day one; rejected the "we'll add L4/L5 later in storage too" version because of refactor risk. Compromise: storage + resolver for 5, UI for 3 in V1. |
| **Store L1 platform defaults in `tenant_config` too** | Adds noise; platform defaults are a single source of truth governed by code review on `contracts/policy.py`, not by tenant edits. |
| **Single config blob per tenant (no per-layer rows)** | Loses authority separation; loses per-layer observability; loses the ability to roll back one layer without touching others. |
| **Use `OrderEvent.metadata` as the override carrier** | Couples per-event payload to per-tenant policy. No way to audit policy changes. Rejected. |
| **Defer the entire override hierarchy to V2** | Breaks the calibration-deferral premise (ADR-032). Customers have nowhere to land calibrated values. Hard reject. |
| **`ConfigChange.scope` as untyped dict** | DDD lens objection: that's a generic event with a payload bag, not a typed domain event. Revised to discriminated union per 2026-05-10 review. |

---

## Open questions

- Cache invalidation strategy for V2 multi-replica deployment: pub/sub (Redis pub/sub vs Postgres `LISTEN`/`NOTIFY`) — decided when V2 scale-out work begins. V1 ships with single-replica process-local TTL-60s + same-process cache-bust on promote.
- Whether `customer_behavior_overrides` (blanket_po etc.) should be first-class typed records in the schema (named presets table) or just a documentation convention. V1.5 decision.
- Multi-tenant template sharing (a customer who's a tenant of multiple manufacturers may want shared baseline config). Out of scope for V1; will need its own ADR.

---

## References

- `gateways/configs/duplicate_po/defaults.json`
- `docs/specs/duplicate-po/calibration-methodology.md`
- `docs/specs/duplicate-po/2026-05-03-design-review.md` (Item 3)
- `docs/specs/duplicate-po/2026-05-10-adr-review.md` (revisions)
- ADR-028, ADR-029, ADR-023 (audit chain), ADR-032
