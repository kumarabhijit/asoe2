# ASOE Exception Schema v0.1 — the dual-track contract

**Status:** Draft for review (v0.1)
**Date:** 2026-04-19
**Owner:** Platform engineering
**Supersedes:** none (first schema commit)
**Related strategy:** [`docs/strategy/2026-04-wedge-commit.md`](../strategy/2026-04-wedge-commit.md)
**Related specs:** [`docs/specs/duplicate-po-product-spec.md`](./duplicate-po-product-spec.md) (must conform to this envelope — see §13)

---

## 1. Purpose & scope

### 1.1 Why this doc exists

ASOE's strategy commit (§6 of the wedge-commit doc) elects a **dual-track geography** from day one — Indian CPG (CavinKare: scheme disputes, dealer back-orders, cold-chain) plus US retail (Walmart/Kroger: OTIF risk, deduction risk). That plan has one load-bearing technical assumption:

> **All exceptions — regardless of geography, ERP, trade-partner class, or exception type — must be expressible in the same record shape.**

This document defines that shape. It is the contract that makes dual-track viable. If we cannot represent a CavinKare scheme dispute and a Walmart OTIF risk in the same schema, the strategy is broken and dual-track collapses.

It is also the contract that **every future exception type** (including the existing duplicate-PO spec) must conform to.

### 1.2 What this document IS

- A normative definition of the envelope every ASOE exception record must populate
- An enumerated v1 catalog of exception types, with per-type payload schemas
- A set of sub-schemas for reason chains, risk quantification, data lineage, lifecycle, and trade partners
- An extension protocol for adding new exception types without breaking existing consumers
- A schema evolution policy (semver + compatibility rules)

### 1.3 What this document is NOT

- **Not** a wire format. JSON Schema here is the normative spec; implementations may serialize to JSON, Avro, or Protobuf as long as they round-trip losslessly.
- **Not** a database schema. Storage layout is an implementation concern. The contract is the record.
- **Not** an action/resolution API. This schema describes what ASOE *observes and scores*, not what it *does*. Resolution workflows, approvals, and writes are out of scope for v0.1 (consistent with the read-only posture in the strategy commit).
- **Not** a UI contract. How exceptions are *presented* is separately specified per exception type.

### 1.4 Consumers of this contract

| Consumer | Uses the schema to… |
|---|---|
| Detection engines (SAP read, Oracle read, EDI parsers) | Emit conforming records |
| Exception digest service (Slack/Teams/email) | Render human-readable summaries |
| `asoe-ui` exception detail views | Power drill-down and reason-chain visualization |
| Auditors (customer-side internal audit, future SOC2) | Trace any alert to its source data with full reproducibility |
| Downstream agents (v0.2+ action layer) | Consume exceptions as input to resolution workflows |
| Customer data teams | Pull exception streams into their own warehouses |

---

## 2. Design principles

Six invariants the schema must honor. If a proposed change violates any of these, it requires explicit sign-off in the ADR log before landing.

### 2.1 One envelope, many payloads

Every exception record has the same top-level fields (§3). Type-specific details live in a `payload` object whose shape depends on `exception_type`. This is a tagged-union pattern. Adding a new exception type must NOT change the envelope.

### 2.2 Read-only by construction

No field implies or requires a write to a source system. Fields like `recommended_action` or `resolution_state` exist, but they describe agent reasoning and downstream workflow state — they do not commit ASOE to executing anything in v0.1. The schema is safe to use in a read-only deployment.

### 2.3 Audit-ready from day one

Every exception must be fully reproducible from its record alone. That requires:
- Source data lineage (which ERP table, which primary key, which snapshot hash — §7)
- Structured reason chain (which rule, which inputs, which output, step-by-step — §5)
- Monetary risk computation method and anchors (§6)
- Immutable lifecycle history (§8)

If an auditor cannot answer *"Why did ASOE raise this exception?"* from the record alone, the schema has failed.

### 2.4 Cross-geo by default

Currency, locale, and region are per-record, not per-deployment. A single ASOE instance can serve a CavinKare exception (₹, IN) and a Walmart OTIF exception ($, US) in the same stream.

### 2.5 Schema evolution is a first-class concern

v0.1 is a commitment to stability for the envelope and for each payload's required fields. Adding optional fields is a minor-version change; removing or re-typing fields is a major-version change that requires a migration path. See §15.

### 2.6 Machine-first, human-legible

The schema is designed for machine validation (JSON Schema) first, but fields are named and structured to be readable by a COO or auditor without a key. `occurrence_timestamp` beats `occ_ts`. `exception_type: SCHEME_DISPUTE_RISK` beats `type_code: 02`.

---

## 3. The envelope

Every exception record — regardless of type — carries these fields. All fields are required unless marked optional.

### 3.1 Envelope fields

| Field | Type | Required | Description |
|---|---|---|---|
| `exception_id` | UUID (string) | yes | Globally unique, stable across re-detections of the same underlying issue. Generated on first detection. |
| `detection_id` | UUID (string) | yes | Unique per detection run. Many detections can share one `exception_id` (e.g., re-scored daily). |
| `schema_version` | semver (string) | yes | Version of this schema the record conforms to (e.g., `"0.1.0"`). |
| `exception_type` | enum | yes | One of the types enumerated in §4. |
| `occurrence_timestamp` | ISO-8601 datetime | yes | When the underlying event happened in the source system (e.g., PO received at `2026-04-19T09:12:03+05:30`). |
| `detection_timestamp` | ISO-8601 datetime | yes | When ASOE detected it. |
| `source_system` | object | yes | ERP identifier sub-schema (§7.1). |
| `source_records` | array<object> | yes | Data lineage — pointers to source rows/objects with snapshot hashes (§7.2). Must have at least one entry. |
| `trade_partner` | object | yes | Who the counterparty is (§9). |
| `severity` | enum | yes | `critical` \| `high` \| `medium` \| `low`. Business impact, not confidence. |
| `confidence` | number `[0.0, 1.0]` | yes | Agent's confidence in the detection. Distinct from severity. |
| `risk` | object | yes | Monetary risk quantification (§6). |
| `reason_chain` | array<object> | yes | Structured reasoning steps (§5). Must have at least one entry. |
| `lifecycle_state` | enum | yes | Current state in the lifecycle (§8). |
| `lifecycle_history` | array<object> | yes | Immutable audit trail of state transitions (§8). Must have at least one entry (the initial `detected` transition). |
| `recommended_action` | enum \| null | optional | Read-only hint for downstream action layer. Null in v0.1 for most types. |
| `autonomy_level` | enum | yes | `L1_OBSERVE` in v0.1 for all records (read-only posture). Reserved for future use. |
| `payload` | object | yes | Type-specific fields (§4 enumerates the allowed shapes). |
| `tags` | array<string> | optional | Free-form labels (customer-defined or system-defined). |
| `tenant_id` | UUID (string) | yes | Multi-tenant isolation key. Enforced at query layer. |

### 3.2 Envelope JSON Schema (normative)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://asoe.io/schemas/exception/v0.1/envelope.schema.json",
  "title": "ASOE Exception Envelope v0.1",
  "type": "object",
  "required": [
    "exception_id", "detection_id", "schema_version", "exception_type",
    "occurrence_timestamp", "detection_timestamp",
    "source_system", "source_records", "trade_partner",
    "severity", "confidence", "risk", "reason_chain",
    "lifecycle_state", "lifecycle_history",
    "autonomy_level", "payload", "tenant_id"
  ],
  "properties": {
    "exception_id": { "type": "string", "format": "uuid" },
    "detection_id": { "type": "string", "format": "uuid" },
    "schema_version": { "type": "string", "pattern": "^\\d+\\.\\d+\\.\\d+$" },
    "exception_type": {
      "type": "string",
      "enum": [
        "FULFILLMENT_RISK",
        "SCHEME_DISPUTE_RISK",
        "BACK_ORDER_AGING",
        "PRICING_MISMATCH",
        "DEDUCTION_RISK",
        "MASTER_DATA_MISMATCH",
        "CUSTOMER_INQUIRY_ANOMALY",
        "DEALER_BACKORDER_RISK",
        "DUPLICATE_PO"
      ]
    },
    "occurrence_timestamp": { "type": "string", "format": "date-time" },
    "detection_timestamp":  { "type": "string", "format": "date-time" },
    "source_system":   { "$ref": "./source-system.schema.json" },
    "source_records":  { "type": "array", "minItems": 1, "items": { "$ref": "./source-record.schema.json" } },
    "trade_partner":   { "$ref": "./trade-partner.schema.json" },
    "severity": { "type": "string", "enum": ["critical", "high", "medium", "low"] },
    "confidence": { "type": "number", "minimum": 0.0, "maximum": 1.0 },
    "risk": { "$ref": "./risk.schema.json" },
    "reason_chain": { "type": "array", "minItems": 1, "items": { "$ref": "./reason-step.schema.json" } },
    "lifecycle_state": {
      "type": "string",
      "enum": ["detected", "alerted", "acknowledged", "human_review", "resolved", "false_positive", "suppressed", "closed", "expired"]
    },
    "lifecycle_history": { "type": "array", "minItems": 1, "items": { "$ref": "./lifecycle-transition.schema.json" } },
    "recommended_action": { "type": ["string", "null"] },
    "autonomy_level": { "type": "string", "enum": ["L1_OBSERVE", "L2_RECOMMEND", "L3_ACT_AND_INFORM", "L4_AUTONOMOUS"] },
    "payload": { "type": "object" },
    "tags": { "type": "array", "items": { "type": "string" } },
    "tenant_id": { "type": "string", "format": "uuid" }
  }
}
```

Type-specific `payload` validation is enforced by a second-stage validator keyed on `exception_type` (§11).

### 3.3 Envelope invariants (enforced at ingestion)

- `detection_timestamp >= occurrence_timestamp`
- `lifecycle_history[0].to_state == "detected"`
- `lifecycle_history[-1].to_state == lifecycle_state`
- `source_records[*].snapshot_timestamp <= detection_timestamp`
- `confidence` is NOT a function of `severity`. A detection can be high-confidence and low-severity, or low-confidence and high-severity. Treating them as coupled is a downstream bug.

---

## 4. Exception type catalog v1

v0.1 enumerates exactly nine exception types. New types require the extension protocol in §14.

| Code | Name | Primary geography | Primary data source | In wedge scope |
|---|---|---|---|---|
| `FULFILLMENT_RISK` | Pre-shipment OTIF risk | US (initially) | EDI 850/856 + SAP inventory | ✅ v0.1 |
| `SCHEME_DISPUTE_RISK` | Dealer scheme claim mismatch | India (initially) | SAP scheme master + dealer claims | ✅ v0.1 |
| `BACK_ORDER_AGING` | Unfulfilled line aging | Both | SAP sales order + inventory | ✅ v0.1 |
| `PRICING_MISMATCH` | Modern-trade pricing drift | Both | SAP pricing conditions + incoming order | ✅ v0.1 |
| `DEDUCTION_RISK` | Post-shipment deduction likelihood | US (initially) | Historical chargeback patterns + shipment data | 🟡 detection only, NO action (Glimpse's lane) |
| `MASTER_DATA_MISMATCH` | Customer/item/price master drift | Both | SAP master tables | ✅ v0.1 |
| `CUSTOMER_INQUIRY_ANOMALY` | Inquiry volume spike per account/item | Both | Support ticket ingestion | 🟡 v0.1 if customer provides ticket feed |
| `DEALER_BACKORDER_RISK` | Distributor/dealer back-order likelihood | India (initially) | SAP distribution + dealer order history | ✅ v0.1 |
| `DUPLICATE_PO` | Duplicate purchase order | Both | EDI 850 + SAP/OMS sales orders | ✅ (existing spec — see §13) |

**Legend:** ✅ = build in v0.1 for first design partners. 🟡 = scope depends on design partner data availability / wedge boundary.

### 4.1 Exception types we deliberately excluded from v0.1

- `INVOICE_MATCH_EXCEPTION` (3-way match) — owned by AP automation vendors, not our wedge
- `CREDIT_HOLD` — banking/finance domain, not order-ops
- `PROMO_PLANNING_VARIANCE` — TPM (trade promotion management) lane, overlaps Confido
- `FORECAST_DEVIATION` — demand planning lane, different buyer (S&OP)
- `TRANSPORTATION_EXCEPTION` — TMS (transportation management) lane
- `RETURN_AUTHORIZATION` — reverse logistics, different workflow surface

These are NOT "never." They are "not in v0.1." Adding any of them post-v0.1 follows the extension protocol (§14).

---

## 5. Reason chain sub-schema

The reason chain is what makes an exception auditable. It is NOT optional and it is NOT free text.

### 5.1 Design intent

An auditor, given only the exception record, should be able to answer:

1. What rule or model produced this detection?
2. What source data was that rule applied to (by exact reference)?
3. What intermediate conclusions did the rule draw?
4. What was the confidence at each step?
5. When was each step evaluated?

### 5.2 Reason step shape

| Field | Type | Required | Description |
|---|---|---|---|
| `step_id` | string | yes | Stable ID within this chain (e.g., `"s1"`, `"s2"`). Order-significant. |
| `rule_id` | string | yes | Stable ID of the rule/model. Format: `<DOMAIN>-<NAME>-<VERSION>` (e.g., `OTIF-WALMART-LEADTIME-v1.2`). |
| `description` | string | yes | Human-readable description of what this step evaluated. One sentence. |
| `inputs` | array<object> | yes | Each input: `{source_ref, field, value}`. `source_ref` points into `source_records` (index or key). |
| `logic` | string | yes | Predicate expression, model name + version, or well-known rule handle. Must be executable-by-inspection, not marketing copy. |
| `output` | any | yes | The conclusion of this step. Type depends on the step. |
| `confidence` | number `[0.0, 1.0]` | yes | Per-step confidence. |
| `evaluated_at` | ISO-8601 datetime | yes | When the step ran. |

### 5.3 Reason step JSON Schema (normative)

```json
{
  "$id": "https://asoe.io/schemas/exception/v0.1/reason-step.schema.json",
  "title": "Reason Step",
  "type": "object",
  "required": ["step_id", "rule_id", "description", "inputs", "logic", "output", "confidence", "evaluated_at"],
  "properties": {
    "step_id":    { "type": "string", "pattern": "^s[0-9]+$" },
    "rule_id":    { "type": "string", "pattern": "^[A-Z][A-Z0-9_-]+-v\\d+\\.\\d+$" },
    "description":{ "type": "string", "minLength": 1, "maxLength": 500 },
    "inputs": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["source_ref", "field", "value"],
        "properties": {
          "source_ref": { "type": "string", "description": "Key or index into envelope.source_records" },
          "field":      { "type": "string" },
          "value":      {}
        }
      }
    },
    "logic":       { "type": "string", "minLength": 1 },
    "output":      {},
    "confidence":  { "type": "number", "minimum": 0.0, "maximum": 1.0 },
    "evaluated_at":{ "type": "string", "format": "date-time" }
  }
}
```

### 5.4 Reason chain rules

- A chain must have at least one step.
- Steps are evaluated in array order.
- A step's `output` may be referenced by a later step's `inputs` via `source_ref: "step:<step_id>"`. This lets a chain compose rules.
- The envelope's top-level `confidence` must equal the final step's `confidence`, OR be explicitly computed by a step with `rule_id: "META-CONFIDENCE-AGG-vX.Y"`.
- `rule_id` versions must match a registered rule in the rule registry (out of scope for this doc, but the registry is the source of truth for what a `rule_id` means).

### 5.5 Anti-patterns (schema is permissive; linters must reject these)

- A reason chain of one step with `logic: "LLM inference"` and no structured inputs — this is a black box, not a reason chain.
- A step whose `inputs` do not resolve to any entry in `source_records` — breaks lineage.
- A step with `confidence: 1.0` and `output` derived from a probabilistic model — overconfidence; clamp.

---

*End of chunk 1. Sections 6–10 (risk, lineage, lifecycle, trade partner, geography) follow in the next commit.*
