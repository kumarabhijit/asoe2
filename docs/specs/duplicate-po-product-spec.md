---
name: b2b-duplicate-po-check
description: >
  Build AI-powered Duplicate Purchase Order (PO) detection and resolution for B2B order management. Use whenever the user
  mentions "duplicate PO", "PO exception", "order deduplication", "PO matching", "duplicate order detection", "B2B order
  resolution", "exception management queue", or order intake validation agents. Also trigger for ERP/OMS PO validation
  patterns (SAP, Oracle, NetSuite, Dynamics), EDI 850 duplicate detection, or approval workflows for supply chain
  exception handling. Covers multi-signal scoring, resolution workflows, autonomy levels, side-by-side PO comparison UX,
  and integration patterns. If the user mentions PRISM or enterprise order management AI agents, check if duplicate PO
  handling is relevant and use this skill alongside other applicable skills.
---

# B2B Order Management Exception Resolution — Duplicate PO Check

## Overview

This skill defines the architecture, logic, and UX patterns for an AI agent that detects and resolves **Duplicate Purchase Order (PO) exceptions** in B2B order management workflows. Duplicate POs are one of the highest-volume, highest-cost exception types in enterprise order-to-cash and procure-to-pay processes — causing revenue leakage, fulfillment errors, and customer disputes.

The agent operates within a broader Order Management Exception Resolution platform, but this skill focuses specifically on the **Duplicate PO detection → triage → resolution** pipeline.

---

## 1. Problem Domain

### What Is a Duplicate PO Exception?

A Duplicate PO exception occurs when an incoming purchase order matches (or near-matches) an existing PO already in the system. Common root causes include:

- **Buyer resubmission** — Customer sends the same PO number via EDI/email/portal after not receiving acknowledgment
- **Multi-channel submission** — Same PO arrives via EDI 850 AND email AND portal upload
- **System retries** — Integration middleware (MuleSoft, Boomi, etc.) retransmits due to timeout/error
- **Intentional re-use** — Buyer reuses a PO number for a new order (common in SMB accounts)
- **Amended PO** — Buyer sends a revised PO with the same number but different line items, quantities, or pricing
- **Cross-subsidiary collision** — Same PO number from different ship-to or bill-to entities under the same parent account

### Why It Matters

| Impact Area           | Consequence of Unresolved Duplicates                        |
|----------------------|-------------------------------------------------------------|
| Revenue              | Double shipments → write-offs, returns, margin erosion      |
| Cash Flow            | Double invoicing → payment disputes, DSO inflation          |
| Customer Trust       | Fulfillment errors damage B2B relationships                 |
| Operations           | Manual review queues grow; CSR/ops teams overwhelmed        |
| Compliance           | SOX/audit findings for order control failures               |

---

## 2. Detection Architecture

### 2.1 Multi-Signal Matching Model

The agent uses a **weighted composite scoring model** to determine duplicate probability. Do NOT rely on PO number alone — enterprise duplicates are frequently fuzzy.

#### Signal Matrix

| Signal                    | Weight | Match Logic                                                                 |
|--------------------------|--------|-----------------------------------------------------------------------------|
| PO Number                | 0.30   | Exact match, normalized (strip leading zeros, whitespace, special chars)    |
| Customer Account ID      | 0.15   | Exact match on sold-to or bill-to party                                     |
| Line Item SKUs           | 0.20   | Jaccard similarity on SKU set (≥0.80 = match)                              |
| Order Total Amount       | 0.10   | Within ±2% tolerance band                                                   |
| Submission Timestamp     | 0.10   | Within configurable window (default: 72 hours)                              |
| Ship-To Address          | 0.05   | Normalized address fuzzy match (Levenshtein ≥ 0.85)                        |
| Submission Channel       | 0.05   | Same vs. different channel (different channel = higher dup likelihood)       |
| Requested Delivery Date  | 0.05   | Within ±3 business days                                                     |

#### Composite Score Thresholds

```
Score ≥ 0.90  →  AUTO-BLOCK    (L4 Autonomy: Agent blocks, notifies customer)
Score 0.70–0.89 →  REVIEW-REQUIRED (L2 Autonomy: Agent flags, human decides)
Score 0.50–0.69 →  SOFT-FLAG     (L1 Autonomy: Agent annotates, order proceeds)
Score < 0.50  →  PASS           (No action, order flows normally)
```

### 2.2 Detection Pipeline

```
Incoming PO (EDI 850 / API / Email / Portal)
       │
       ▼
┌─────────────────────┐
│  1. NORMALIZE        │  Strip whitespace, leading zeros, special chars
│     PO Payload       │  Standardize addresses, SKU formats, dates
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  2. CANDIDATE        │  Query existing POs within lookback window
│     RETRIEVAL        │  Filter by: same customer ± PO number prefix match
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  3. MULTI-SIGNAL     │  Compute weighted composite score against
│     SCORING          │  each candidate PO
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  4. CLASSIFICATION   │  AUTO-BLOCK / REVIEW-REQUIRED / SOFT-FLAG / PASS
│     & ROUTING        │  Route to appropriate queue or auto-action
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│  5. RESOLUTION       │  Agent recommends action + evidence card
│     & ACTION         │  Human approves/overrides/escalates
└─────────────────────┘
```

### 2.3 Lookback Window Configuration

The lookback window defines how far back the agent searches for potential duplicate matches. This should be configurable per customer tier:

| Customer Tier | Default Lookback | Rationale                                     |
|--------------|-----------------|-----------------------------------------------|
| Strategic    | 90 days         | Large accounts may have long procurement cycles |
| Standard     | 30 days         | Typical reorder frequency                      |
| SMB / Spot   | 14 days         | Short cycles, higher PO number reuse risk      |

---

## 3. Resolution Workflows

### 3.1 Resolution Action Types

The agent presents resolution recommendations based on the duplicate classification and context:

| Action                | Description                                                        | When Used                                          |
|----------------------|--------------------------------------------------------------------|----------------------------------------------------|
| **BLOCK_AND_NOTIFY** | Reject the incoming PO; send acknowledgment to buyer with reason   | Auto-block threshold; exact duplicate confirmed     |
| **MERGE**            | Combine incoming PO with existing as an amendment/revision          | Same PO#, different quantities or line items        |
| **SUPERSEDE**        | Replace existing PO with incoming (newer version wins)             | Amended PO with explicit revision indicator         |
| **ALLOW_BOTH**       | Accept both POs as distinct orders                                 | Intentional reorder; different delivery dates/sites |
| **ESCALATE**         | Route to senior analyst or account manager                         | Ambiguous signals; high-value account               |
| **REQUEST_BUYER_CONFIRMATION** | Send inquiry to buyer to clarify intent                | Moderate confidence; buyer behavior unclear          |

### 3.2 Resolution Decision Tree

```
Is PO Number an exact match?
├── YES
│   ├── Is Customer Account the same?
│   │   ├── YES
│   │   │   ├── Are line items identical (SKU + Qty)?
│   │   │   │   ├── YES
│   │   │   │   │   ├── Was original PO already fulfilled/shipped?
│   │   │   │   │   │   ├── YES → ALLOW_BOTH (likely reorder)
│   │   │   │   │   │   └── NO  → BLOCK_AND_NOTIFY (true duplicate)
│   │   │   │   └── NO
│   │   │   │       ├── Does incoming PO have revision indicator?
│   │   │   │       │   ├── YES → SUPERSEDE
│   │   │   │       │   └── NO  → MERGE or REQUEST_BUYER_CONFIRMATION
│   │   │   └── NO (different customer, same PO#)
│   │   │       └── ALLOW_BOTH (PO# collision across accounts)
│   │   └── ...
│   └── ...
├── NO (fuzzy match only)
│   ├── Composite Score ≥ 0.70?
│   │   ├── YES → REVIEW-REQUIRED (present evidence card)
│   │   └── NO  → SOFT-FLAG or PASS
│   └── ...
└── ...
```

### 3.3 Autonomy Levels

Align with the platform's autonomy framework:

| Level | Label            | Agent Behavior                                                       |
|-------|-----------------|----------------------------------------------------------------------|
| L1    | Observe          | Agent flags potential duplicate in dashboard; takes no action         |
| L2    | Recommend        | Agent recommends resolution; human must approve to execute            |
| L3    | Act & Inform     | Agent executes resolution; notifies human post-action                |
| L4    | Full Autonomy    | Agent executes resolution silently; logs for audit                   |

Default autonomy per resolution type:

- `BLOCK_AND_NOTIFY` → L3 (auto-block with notification) or L4 for exact duplicates within 24 hours
- `MERGE` / `SUPERSEDE` → L2 (always requires human approval due to line-item changes)
- `ALLOW_BOTH` → L3 (auto-approve with log)
- `ESCALATE` → L1 (always human-driven)
- `REQUEST_BUYER_CONFIRMATION` → L2 (human reviews before outbound communication)

---

## 4. Data Model

### 4.1 Core Entities

```sql
-- Incoming PO record (staging)
CREATE TABLE incoming_po (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id),
    po_number         VARCHAR(50) NOT NULL,
    po_number_norm    VARCHAR(50) NOT NULL,  -- normalized for matching
    customer_id       UUID NOT NULL REFERENCES customers(id),
    submission_channel VARCHAR(20) NOT NULL,  -- 'EDI', 'API', 'EMAIL', 'PORTAL'
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_amount      NUMERIC(12,2),
    currency          VARCHAR(3) DEFAULT 'USD',
    ship_to_address   JSONB,
    requested_delivery DATE,
    raw_payload       JSONB NOT NULL,        -- original EDI/JSON/XML
    status            VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    -- PENDING | PROCESSING | PASSED | FLAGGED | BLOCKED | RESOLVED
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- PO line items (staging)
CREATE TABLE incoming_po_lines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incoming_po_id  UUID NOT NULL REFERENCES incoming_po(id),
    line_number     INT NOT NULL,
    sku             VARCHAR(50) NOT NULL,
    quantity        NUMERIC(10,2) NOT NULL,
    unit_price      NUMERIC(10,2),
    description     TEXT,
    UNIQUE(incoming_po_id, line_number)
);

-- Duplicate detection results
CREATE TABLE duplicate_check_results (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    incoming_po_id    UUID NOT NULL REFERENCES incoming_po(id),
    matched_po_id     UUID,                  -- existing PO in OMS
    composite_score   NUMERIC(4,3) NOT NULL,  -- 0.000 to 1.000
    signal_breakdown  JSONB NOT NULL,         -- per-signal scores
    classification    VARCHAR(20) NOT NULL,
    -- AUTO_BLOCK | REVIEW_REQUIRED | SOFT_FLAG | PASS
    recommended_action VARCHAR(30),
    -- BLOCK_AND_NOTIFY | MERGE | SUPERSEDE | ALLOW_BOTH | ESCALATE | REQUEST_BUYER_CONFIRMATION
    agent_reasoning    TEXT,                  -- LLM-generated explanation
    autonomy_level     VARCHAR(5),           -- L1, L2, L3, L4
    resolved_by        UUID REFERENCES users(id),
    resolved_action    VARCHAR(30),          -- actual action taken
    resolved_at        TIMESTAMPTZ,
    resolution_notes   TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit log for compliance
CREATE TABLE duplicate_check_audit (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    check_result_id   UUID NOT NULL REFERENCES duplicate_check_results(id),
    action            VARCHAR(50) NOT NULL,
    actor_type        VARCHAR(10) NOT NULL,  -- 'AGENT' or 'HUMAN'
    actor_id          VARCHAR(100),
    details           JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 4.2 Indexes for Performance

```sql
-- Fast lookback queries
CREATE INDEX idx_incoming_po_customer_date
    ON incoming_po (customer_id, submitted_at DESC)
    WHERE status NOT IN ('BLOCKED', 'RESOLVED');

CREATE INDEX idx_incoming_po_norm
    ON incoming_po (po_number_norm, tenant_id);

-- Exception queue
CREATE INDEX idx_dup_check_pending
    ON duplicate_check_results (classification, created_at)
    WHERE resolved_at IS NULL;
```

---

## 5. API Design

### 5.1 Key Endpoints

```
POST   /api/v1/orders/inbound              # Receive incoming PO
GET    /api/v1/exceptions/duplicates        # List duplicate exceptions (queue)
GET    /api/v1/exceptions/duplicates/:id    # Get exception detail + evidence card
POST   /api/v1/exceptions/duplicates/:id/resolve   # Submit resolution action
GET    /api/v1/exceptions/duplicates/stats  # Dashboard metrics
PUT    /api/v1/config/duplicate-detection   # Update detection thresholds/weights
```

### 5.2 Inbound PO Processing (Core Flow)

```
POST /api/v1/orders/inbound
Content-Type: application/json

{
  "po_number": "PO-2026-00482",
  "customer_id": "cust_9f8e7d6c",
  "channel": "EDI",
  "lines": [
    { "sku": "WG-CEREAL-001", "qty": 500, "unit_price": 3.49 },
    { "sku": "WG-CEREAL-002", "qty": 200, "unit_price": 4.29 }
  ],
  "ship_to": {
    "name": "Metro Distribution Center",
    "street": "1200 Industrial Pkwy",
    "city": "Dallas",
    "state": "TX",
    "zip": "75201"
  },
  "requested_delivery": "2026-03-25",
  "total": 2603.00
}
```

**Response (duplicate detected):**

```json
{
  "order_id": "ord_abc123",
  "status": "FLAGGED",
  "duplicate_check": {
    "classification": "REVIEW_REQUIRED",
    "composite_score": 0.847,
    "matched_order": "ord_xyz789",
    "recommended_action": "BLOCK_AND_NOTIFY",
    "autonomy_level": "L2",
    "evidence_summary": "Exact PO number match with same customer. Line items are identical. Original PO submitted 4 hours ago via Portal and is currently in fulfillment queue.",
    "signals": {
      "po_number": { "score": 1.0, "detail": "Exact match after normalization" },
      "customer_id": { "score": 1.0, "detail": "Same sold-to party" },
      "line_items": { "score": 1.0, "detail": "Identical SKU set and quantities" },
      "amount": { "score": 1.0, "detail": "Exact total match ($2,603.00)" },
      "timestamp": { "score": 0.92, "detail": "Submitted 4.2 hours after original" },
      "ship_to": { "score": 0.98, "detail": "Address match (minor formatting diff)" },
      "channel": { "score": 0.80, "detail": "Different channel (EDI vs Portal)" },
      "delivery_date": { "score": 1.0, "detail": "Same requested delivery date" }
    }
  }
}
```

### 5.3 Resolution Endpoint

```
POST /api/v1/exceptions/duplicates/:id/resolve

{
  "action": "BLOCK_AND_NOTIFY",
  "notes": "Confirmed duplicate — buyer's EDI system retransmitted after portal submission.",
  "notify_buyer": true,
  "notification_template": "duplicate_po_blocked"
}
```

---

## 6. UX Patterns

### 6.1 Exception Queue View

Follow the application-design framework: **Agent is primary, human is decision authority.**

The exception queue should present:

**Top-Level Dashboard Strip:**
- Total open exceptions (today / this week)
- Auto-resolved count (demonstrates agent value)
- Average resolution time
- Exceptions by classification breakdown (donut chart)

**Queue Table (Default View):**

Each row shows:
- Exception severity indicator (color-coded: red = auto-block, amber = review, blue = soft-flag)
- PO Number + Customer Name
- Composite Score (as a confidence bar, not raw number)
- Recommended Action (as a pill/badge)
- Time in Queue
- Quick-action buttons: Approve Recommendation | Override | Escalate

**Exception Detail (Click-Through):**

Two-layer cognition model:

**Layer 1 — Executive Summary:**
- Agent recommendation with confidence level
- 2–3 key evidence signals (e.g., "Exact PO match", "Same customer", "Submitted 4hrs apart")
- One-click action buttons

**Layer 2 — Evidence Deep Dive (Expandable):**
- Side-by-side comparison of incoming PO vs. matched PO
- Full signal breakdown with individual scores
- Order timeline (when each PO was submitted, via which channel)
- Agent reasoning trace (collapsed by default)
- Customer order history context (last 10 orders)

### 6.2 Side-by-Side PO Comparison

Critical for human reviewers. Display:

```
┌─────────────────────────┐  ┌─────────────────────────┐
│  INCOMING PO            │  │  EXISTING PO (MATCHED)  │
│  PO-2026-00482          │  │  PO-2026-00482          │
│  Via: EDI 850           │  │  Via: Portal Upload     │
│  Submitted: 2:47 PM     │  │  Submitted: 10:15 AM    │
│                         │  │                         │
│  Lines:                 │  │  Lines:                 │
│  WG-CEREAL-001 × 500   │  │  WG-CEREAL-001 × 500   │
│  WG-CEREAL-002 × 200   │  │  WG-CEREAL-002 × 200   │
│                         │  │                         │
│  Total: $2,603.00       │  │  Total: $2,603.00       │
│  Ship To: 1200 Indus... │  │  Ship To: 1200 Indus... │
│  Delivery: 2026-03-25   │  │  Delivery: 2026-03-25   │
└─────────────────────────┘  └─────────────────────────┘

         Differences highlighted in amber
         Matches shown with ✓ indicators
```

### 6.3 Agent Reasoning Card

Display the agent's explanation as a calm, structured card — not a chat transcript:

```
┌──────────────────────────────────────────────────────┐
│  🤖 Agent Assessment              Confidence: 94.7%  │
│─────────────────────────────────────────────────────│
│                                                      │
│  RECOMMENDATION: Block & Notify Buyer                │
│                                                      │
│  This appears to be a true duplicate. The buyer's    │
│  EDI system submitted the same order 4 hours after   │
│  a portal upload — likely an automated retry after   │
│  the portal acknowledgment wasn't received by their  │
│  ERP.                                                │
│                                                      │
│  Key Evidence:                                       │
│  • Exact PO number match                             │
│  • Identical line items and quantities               │
│  • Same delivery date and ship-to                    │
│  • Different submission channel (EDI vs Portal)      │
│                                                      │
│  Similar Pattern: This buyer had 3 similar duplicate  │
│  submissions in the last 90 days.                    │
│                                                      │
│  [✓ Approve]  [✎ Override]  [↗ Escalate]            │
└──────────────────────────────────────────────────────┘
```

---

## 7. Integration Patterns

### 7.1 ERP/OMS Systems

The duplicate check agent must integrate with the system of record for existing POs:

| System              | Integration Method                  | Notes                              |
|--------------------|-------------------------------------|------------------------------------|
| SAP S/4HANA        | RFC/BAPI via MuleSoft or SAP CPI    | BAPI_SALESORDER_GETLIST            |
| Oracle EBS / Fusion | REST API or DB link                 | PO_HEADERS_ALL view                |
| NetSuite           | SuiteTalk REST API                  | TransactionSearch                  |
| Microsoft Dynamics  | OData / Dataverse API               | SalesOrderHeaders entity           |
| Custom OMS         | Direct PostgreSQL / API             | Query existing orders table        |

### 7.2 EDI Pipeline Integration

For EDI 850 (Purchase Order) inbound:

```
EDI Gateway (e.g., SPS Commerce, TrueCommerce)
    │
    ▼
Translation Layer (X12 850 → JSON)
    │
    ▼
Integration Middleware (MuleSoft / Boomi / Workato)
    │
    ▼
Duplicate Check Agent (this skill)
    │
    ├── PASS → Forward to OMS for fulfillment
    ├── FLAGGED → Route to exception queue
    └── BLOCKED → Send 997 FA + notification to buyer
```

### 7.3 Notification Templates

Pre-built notification templates for buyer communication:

- **duplicate_po_blocked** — "Your PO {po_number} was not processed because it matches an existing order {existing_po} submitted on {date}. If this is a new order, please submit with a unique PO number."
- **duplicate_po_inquiry** — "We received PO {po_number} which appears similar to an existing order. Could you confirm whether this is a new order or a resubmission?"
- **duplicate_po_amended** — "We received a revised version of PO {po_number}. The original order has been updated with your changes."

---

## 8. Metrics & Observability

### 8.1 Key Metrics to Track

| Metric                        | Target           | Description                                    |
|------------------------------|------------------|------------------------------------------------|
| Duplicate Detection Rate     | > 95%            | % of true duplicates caught by agent           |
| False Positive Rate          | < 5%             | % of non-duplicates incorrectly flagged        |
| Auto-Resolution Rate         | > 60%            | % of duplicates resolved without human action  |
| Mean Time to Resolution      | < 15 min         | From detection to resolution                   |
| Queue Depth                  | < 50 open        | Number of unresolved exceptions at any time    |
| Buyer Dispute Rate           | < 1%             | Post-resolution disputes from buyers           |

### 8.2 Feedback Loop

Every human override of an agent recommendation feeds back into the model:

```
Human overrides "BLOCK_AND_NOTIFY" → "ALLOW_BOTH"
    │
    ▼
Log override with context (reason, customer, signals)
    │
    ▼
Periodic retraining / threshold adjustment
    │
    ▼
Agent improves future recommendations
```

---

## 9. Configuration & Tuning

### 9.1 Tenant-Level Configuration

All detection parameters should be configurable per tenant:

```json
{
  "duplicate_detection": {
    "enabled": true,
    "lookback_window_days": 30,
    "score_weights": {
      "po_number": 0.30,
      "customer_id": 0.15,
      "line_items": 0.20,
      "amount": 0.10,
      "timestamp": 0.10,
      "ship_to": 0.05,
      "channel": 0.05,
      "delivery_date": 0.05
    },
    "thresholds": {
      "auto_block": 0.90,
      "review_required": 0.70,
      "soft_flag": 0.50
    },
    "amount_tolerance_pct": 2.0,
    "timestamp_window_hours": 72,
    "delivery_date_tolerance_days": 3,
    "auto_block_autonomy": "L3",
    "review_autonomy": "L2",
    "customer_tier_overrides": {
      "strategic": { "lookback_window_days": 90 },
      "smb": { "lookback_window_days": 14 }
    }
  }
}
```

### 9.2 Customer-Level Overrides

Some customers may need special handling:

- **Blanket PO customers** — Same PO number used across multiple releases; disable PO number signal or reduce weight
- **Drop-ship accounts** — Different ship-to per order is normal; reduce ship-to weight
- **High-frequency reorder accounts** — Tighten timestamp window; increase line-item weight

---

## 10. Implementation Guidance

### 10.1 Tech Stack Recommendations

| Layer              | Recommended                                  |
|-------------------|----------------------------------------------|
| Backend API       | Node.js/Express or Python/FastAPI            |
| Database          | PostgreSQL with JSONB for flexible payloads  |
| Queue / Events    | Redis Streams or Apache Kafka                |
| AI/LLM Layer      | Claude API (reasoning, explanation gen)       |
| Frontend          | React + Tailwind (following app-design skill)|
| Auth              | JWT with role-based access (Admin, Analyst, Viewer) |
| Integration       | MuleSoft / Boomi for ERP connectivity        |

### 10.2 Build Sequence

1. **Data model & migrations** — Set up PostgreSQL schema (Section 4)
2. **Normalization engine** — PO number, address, SKU normalization functions
3. **Candidate retrieval** — Efficient lookback query with proper indexing
4. **Scoring engine** — Multi-signal weighted scorer (Section 2.1)
5. **Classification & routing** — Threshold-based classification (Section 2.1)
6. **Resolution API** — CRUD for exception queue + resolution actions (Section 5)
7. **Agent reasoning** — Claude API integration for explanation generation
8. **Exception queue UI** — Dashboard + queue + detail views (Section 6)
9. **Notification service** — Buyer communication templates (Section 7.3)
10. **Metrics & feedback loop** — Observability + override logging (Section 8)

### 10.3 Testing Strategy

- **Unit tests** — Scoring engine with known signal combinations
- **Integration tests** — Full pipeline from PO intake to classification
- **Edge case tests** — Blanket POs, cross-subsidiary collisions, amended POs
- **Load tests** — Simulate high-volume EDI intake (1000+ POs/hour)
- **Human-in-the-loop tests** — Verify UX flow for analyst review and override

---

## 11. Security & Compliance

- All PO data encrypted at rest (AES-256) and in transit (TLS 1.3)
- Role-based access: only authorized analysts can resolve exceptions
- Full audit trail on every detection, recommendation, and resolution action
- SOX-compliant logging with tamper-evident audit records
- Data retention policies aligned with customer contracts and regulatory requirements
- PII handling: buyer contact info stored separately with access controls
- Multi-tenant isolation: tenant_id enforced at query level (RLS recommended)

---

## 12. Glossary

| Term        | Definition                                                                 |
|-------------|---------------------------------------------------------------------------|
| PO          | Purchase Order — buyer's formal request to purchase goods/services         |
| EDI 850     | Electronic Data Interchange standard for purchase orders                   |
| OMS         | Order Management System                                                    |
| Sold-To     | The customer entity placing the order                                      |
| Ship-To     | The physical delivery destination                                          |
| Bill-To     | The entity responsible for payment                                         |
| Jaccard     | Set similarity metric: |A∩B| / |A∪B|                                     |
| DSO         | Days Sales Outstanding — measure of accounts receivable collection speed   |
| RLS         | Row-Level Security — database-enforced tenant isolation                    |
| 997 FA      | EDI Functional Acknowledgment — confirms receipt/rejection of a transaction|
