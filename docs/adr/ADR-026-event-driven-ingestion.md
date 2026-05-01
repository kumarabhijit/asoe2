# ADR-026: Event-driven ingestion via Azure Event Hubs (Phase B)

**Status:** Proposed
**Date:** 2026-05-01
**Deciders:** Principal AI Systems Architect; Platform; Compliance (review pending)
**Applies to:** `api/routes/exceptions.py`, new `connectors/`, new
`api/ingest/` module, `infra/main.bicep`, `docs/deploy-azure-container-apps.md`

---

## Context

The current ingestion entry point is `POST /api/v1/exceptions/resolve`.
Every external event — SAP order published an EDI 850 mismatch, an
Oracle EBS delivery slipped, a Salesforce credit-block flag, an
inbound email about a duplicate PO — has to land here for the
deterministic Skill→Shadow→Recipe pipeline to do its work.

That contract is the right ingestion shape: a single canonical entry
point, every event runs the same compliance routing, every record
gets an audit chain. What's missing is **how events get there from
the source systems** without modifying SAP / Oracle / Salesforce.

The user's framing on this is correct: ASOE has to **augment**
existing ERPs, not replace them. The connector pattern below — one
small adapter per source system, all emitting a canonical event to a
shared bus — is the standard answer (Hohpe & Woolf *Enterprise
Integration Patterns*: Translator + Canonical Data Model; Confluent
event-driven microservices playbook; Microsoft's first-party guidance
for Service Bus / Event Hubs / Event Grid; SAP's own Event Mesh
positioning).

Two distinct integration patterns surface in a full deployment, and
they MUST not be conflated:

```
                    INGESTION (push, async)
[SAP S/4 events]        ──→ [SAP connector]   ──┐
[Oracle EBS]            ──→ [Oracle connector] ──┤
[Salesforce CDC events] ──→ [SFDC connector]   ──┤  ──→  [Event Hub] ──→ [asoe-core consumer] ──→ same internal handler as /resolve
[M365 mailbox]          ──→ [Email parser]     ──┤
[EDI 850/856 (VAN)]     ──→ [EDI connector]    ──┘

                    ENRICHMENT (pull, sync, during recipe execution)
[asoe-core recipe]  ──→ [oms gateway]      ──→ [SAP OData]
                  │ ──→ [sap_doc gateway]   ──→ [SAP IDoc]
                  │ ──→ [sap_contract gw]   ──→ [SAP CRM]
                  │ ──→ [salesforce gw]     ──→ [SFDC REST]
                  └ ──→ [...pluggable]      ──→ [any external read API]

                    OUTBOUND ACTION (write-back via gateway)
[recipe decides "release price hold"]   ──→ [sap_block gateway]    ──→ [SAP Block release IDoc]
[recipe decides "notify buyer"]         ──→ [buyer_notification]   ──→ [M365 SendMail / Slack]
```

* **Ingestion** is push, async, eventually-consistent. Source systems
  publish business events to their native channels (SAP S/4 Event
  Mesh, Salesforce Platform Events / CDC, Oracle Integration Cloud,
  IMAP/Graph webhooks for email). One small connector per source
  subscribes, translates to the canonical asoe `OrderEvent` shape,
  publishes to a shared bus.

* **Enrichment + outbound action** is pull, synchronous, called by
  recipes during `/resolve` execution. The existing `gateways/`
  module is the right surface — sandbox stubs today, production
  HTTP/RFC clients in Phase B+. Recipes don't change.

The user's question conflated these two. The bus only carries the
ingestion-direction traffic.

## Decision

Phase B ships event-driven ingestion against **Azure Event Hubs**
(Kafka-protocol surface), with **one source connector** as the proof
point and the bus consumer wired into the same internal handler that
`POST /api/v1/exceptions/resolve` calls.

### Rationale per layer

| Layer | Choice | Why over alternatives |
|---|---|---|
| **Bus** | Azure Event Hubs (Kafka-protocol mode) | Same Azure tenant as Container Apps + Postgres + Redis (no cross-cloud egress for SOX-relevant payloads); Kafka protocol means connectors written here run unchanged against Confluent / MSK if we ever leave Azure; built-in 7-day replay; built-in Schema Registry (Avro/JSON/Protobuf). Service Bus is queue-shaped (good for command-pattern, wrong for streaming). Event Grid is router-shaped (good for fan-out, wrong for ordered replay). |
| **Schema** | Canonical `OrderEvent` mirroring `contracts/models.py::OrderEvent` (the same shape `/resolve` accepts today) | One source of truth; the bus consumer can call the existing handler verbatim. Source-specific fields go in `metadata`. Avro registered for evolution. |
| **First connector** | M365 mailbox via Microsoft Graph webhook → IMAP `OrderEvent` extractor | Lowest implementation cost (no SAP licence, no Oracle Integration Cloud setup). Demonstrates the connector pattern end-to-end without taking on a vendor dependency on day 1. SAP / Salesforce / Oracle land as separate ADRs once the pattern is proven. |
| **Identity** | Event Hubs uses Entra ID, container-side managed identity (UAMI), no connection-string secrets | Same posture as the existing UAMI for ACR pull (`infra/main.bicep:uami`). One identity, RBAC-scoped per-topic. |
| **Persistence on the asoe-core side** | The bus consumer creates an `Exception` row exactly the way `/resolve` does today; same audit chain, same Postgres rows, same WS event publication | The internal handler is the single source of truth — REST and bus paths both call it. No second audit trail. |
| **Outbound (writes back to source systems)** | Stays as gateway adapters (sync, called by recipes). NOT on the bus. | The recipe needs to know whether the action succeeded before it can finish; async write-back would break the deterministic path. Out-of-band telemetry (metrics, audit) can go on a separate topic in a later phase. |

### What this ADR explicitly does NOT decide

* The full set of source connectors. Each ERP / Salesforce / EDI
  integration gets its own ADR once the connector pattern is locked
  in via the email connector.
* Outbound write-back over the bus. Stays gateway-shaped per above.
* Cross-tenant routing. Multi-tenant on the bus is a separate
  decision; for pre-prod we have a single tenant (`acme-corp`).
* Per-node timing capture for the WaterfallStepper (see "Deferred"
  below).

## Phased rollout

### Phase B.1 — Bus + email connector (this ADR's scope)

1. Bicep: `Microsoft.EventHub/namespaces` + `eventhubs/asoe-events`
   topic + Schema Registry. Managed identity gets `Azure Event Hubs
   Data Receiver` and `Schema Registry Reader` for the connector
   identity, `Azure Event Hubs Data Sender` for the producer.
2. New `connectors/email_m365/` Container App. Subscribes to a Graph
   change-notification webhook, parses inbound mail with a small
   templated extractor, publishes one canonical `OrderEvent` per
   recognised exception keyword, drops everything else. Independent
   deploy lifecycle from asoe-core.
3. New `api/ingest/bus_consumer.py` module in asoe-core. Long-running
   asyncio task that reads from the Event Hub, validates against the
   schema, and calls the same internal handler `/resolve` invokes.
   Mounted as a startup task on the API Container App.
4. New `scripts/seed-bus.py` operator helper to publish synthetic
   events (mirroring the smoke fixture set) onto the bus, so the
   existing `smoke-e2e.sh` REST path has a bus-driven sibling that
   exercises the same fixtures.
5. Tests:
   * `tests/test_bus_consumer.py` — handler round-trip with a
     mocked event reader, asserts the same audit-chain row a `/resolve`
     POST would produce.
   * `tests/test_canonical_event_schema.py` — Avro/JSON schema is the
     same shape as `OrderEvent`.
   * `tests/test_email_extractor.py` — fixture inboxes →
     `OrderEvent` objects.

**Cost estimate:** Event Hubs Standard SKU at 1 throughput unit is
~$22/mo + ~$0.028/million events. For sandbox volume (single-digit
events/min), this is comfortably under $30/mo. Schema Registry is
free at low volume.

**Effort:** ~4-5 days end-to-end including bicep + tests. Tracked as
a separate branch `phase_b_bus_ingestion`.

### Phase B.2 — Per-node pipeline timings (deferred from session 2026-05-01)

The WaterfallStepper currently shows lifecycle-derived completion
status with no per-node durations (`duration_ms` honestly absent —
the previous Math.random fabrication is gone, see asoe-ui commit
`1dcf026`). Real timings require:

1. `orchestration/graph.py`: wrap each LangGraph node entry/exit with
   a `time.perf_counter()` measurement; emit a
   `WSEvent.pipeline_progress` per node with `duration_ms` populated.
2. New persisted projection: a `pipeline_node_records` table OR
   extend `traces.trace_record` with a `completed_nodes` array
   captured during execution.
3. `api/schemas.py::TraceResponse`: add `completed_nodes:
   List[PipelineNodeRecord] = Field(default_factory=list)`.
4. `asoe-ui/src/app/exceptions/shared.tsx::buildNodeStates`: when
   `trace.completed_nodes` is non-empty, project from it; fall
   back to lifecycle synthesis only for legacy traces.

**Why deferred from the 2026-05-01 session:** the orchestrator
emission is the actual gap (the `WSEvent.pipeline_progress` factory
exists but no code calls it). UI plumbing without that emission is
plumbing without payload. Separate session, separate ADR (this one
just records the intent so it doesn't get lost).

### Phase B.3 — Additional source connectors

Each gets its own ADR:

* **SAP S/4 Event Mesh** → `connectors/sap_s4/` — subscribes to
  business events (CO_ORDER_RELEASED, INVOICE_BLOCKED, etc.).
  Highest-value but requires SAP Integration Suite licence.
* **Salesforce Platform Events / CDC** → `connectors/salesforce/` —
  subscribes to per-object CDC streams.
* **Oracle Integration Cloud** → `connectors/oracle_oic/`.
* **EDI 850/856 (VAN)** → `connectors/edi/` — pulls from a VAN
  endpoint, ANSI X12 → canonical event.

Each rolls out independently; the bus stays the same.

## Consequences

### Positive

* **One audit boundary.** Every event whether REST-injected or
  bus-injected goes through the same internal handler; one audit
  chain, one set of compliance gates, one tenant RBAC.
* **Source systems unchanged.** SAP/Oracle/Salesforce administrators
  don't see asoe; they just continue publishing to their native event
  channels. Connectors do all the translation.
* **Pluggability is enforced.** Adding a new ERP = ship a connector
  + register a schema. No change to recipes, gateways, orchestration,
  or the asoe-core API.
* **Replay capability.** Event Hubs' 7-day retention lets us re-run
  ingestion from a past point if a recipe bug got past Compliance
  Shadow. The audit chain stays append-only (recipes are
  deterministic; replay produces the same outcome).
* **Determinism preserved.** The bus consumer calls the same internal
  handler as `/resolve`. If you can `POST /resolve` it deterministically
  through the smoke test today, you can replay it from the bus
  deterministically tomorrow.

### Negative

* **More operational surface.** Event Hubs namespace, throughput
  units, schema registry, DLQ topic, connector lifecycle, identity
  RBAC. Each is a thing that can be misconfigured. Mitigation:
  bicep-managed end-to-end; runbook entries in
  `docs/deploy-azure-container-apps.md`.
* **Eventual consistency.** A bus-injected event is queued and
  processed asynchronously; the source system has no acknowledgement
  that asoe-core ran. For the SOX-relevant decisions asoe-core
  produces, this is fine — the audit chain is the system of record,
  not the source. But it does mean operators and source-system
  administrators need to understand that "the message was sent" ≠
  "the action was taken".
* **Tighter platform coupling.** Connectors are coupled to vendor
  event schemas (SAP Event Mesh names, Salesforce CDC channel
  names, Graph webhook payloads). Mitigation: each connector is
  isolated in its own module; the canonical event sat between them
  is the stable contract.

### Compliance notes

* `metadata.source_system`, `metadata.source_event_id`,
  `metadata.received_at` MUST be populated on every event the bus
  consumer creates. That's how the audit chain captures *which
  connector* sourced the event and *which schema version* was in
  effect — required for the Verdict Pillar 6 reproducibility
  requirement.
* `metadata.synthetic` (per the `tests/fixtures/synthetic/`
  convention) extends to the bus path: the seed script tags events
  the same way so the audit chain can distinguish smoke traffic
  from real ingest. The smoke fixtures are not a special case —
  they ARE the bus contract test, just with `synthetic=true`.
* The Schema Registry version travels with each event; auditors can
  reproduce a record's resolution by re-running the recipe against
  the recorded payload using the matching schema version.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Direct REST POST from each source system** | Requires modifying SAP/Oracle/Salesforce. Violates the user's "augment, don't change" requirement. |
| **Polling integration** (asoe-core polls source systems) | Creates load against transactional ERPs, has lag, doesn't replay. Source-system events are designed to be subscribed to, not polled. |
| **Service Bus instead of Event Hubs** | Service Bus is queue-shaped — point-to-point, no replay, no streaming throughput. We need streaming because (a) high-throughput SAP event channels exist, (b) we want replay for audit. |
| **Build our own messaging layer** | Re-inventing the wheel. Event Hubs + Kafka protocol is the boring, well-tested choice. |
| **Single shared connector for all source systems** | Couples vendor schemas. Each source's event format evolves independently; one connector per source is the loose-coupling pattern. |

## Open questions

* Multi-tenant routing on the bus. Single-tenant works for pre-prod;
  multi-tenant deferred to a Phase B.4 ADR.
* Outbound action over the bus (vs. gateway). Gateway-shaped today
  per "What this ADR explicitly does NOT decide"; revisit when a
  recipe legitimately needs async write-back semantics.
* Schema evolution policy. Avro forward+backward compatibility is
  the default; we'll codify the deprecation timeline once a real
  schema change lands.
