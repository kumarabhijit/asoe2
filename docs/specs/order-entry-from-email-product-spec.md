# ASOE Order Management — Order Entry from Email (Product Spec)

> **Status:** Product Owner specification. Reference content only — not runtime code.
> **Saved here per** `prompts/po-spec-to-asoe.md` STEP 6: PO specs do not live in `skills/`;
> they are preserved verbatim under `docs/specs/` and the runtime architecture is carved
> out separately into a `*_SKILL.md` (reasoning), `*Recipe.py` (deterministic execution),
> and (where infrastructure I/O is required) gateway dependencies.
> **Bucketed mapping:** see `docs/adr/ADR-034-email-order-entry-skill.md`.
> **Original verbatim source:** PO message dated 2026-05-04.

---

## 1. Overview
This skill defines the architecture, logic, and UX patterns for an AI agent that extracts, validates, and submits customer sales orders arriving through non-EDI email channels. These orders typically account for a long tail of manual CSR work involving hours of transcription and exception-handling.

The agent operates within the **ASOE (Agentic System of Engagement)** platform, immediately downstream of the `email-intelligence-agent` (which classifies inbound email intent) and upstream of the ERP's sales-order creation layer.

### Skill Metadata
* **Name:** `asoe-om-order-entry`
* **Type:** DOMAIN
* **Description:** Extract, validate, and submit customer sales orders from email bodies, PDF attachments (native/scanned), Excel workbooks, or images.

### Use Cases & Triggers
Use this skill when the user mentions:
* "email order", "order from email", "customer sent an order", "PO in a PDF".
* "order extraction", "parse an order", "non-EDI order intake", "manual order entry".
* Discussion of converting unstructured communications into ERP sales orders (including downstream validation like duplicate PO, MOQ, pricing, credit, etc.).

**Routing Notes:**
* If the user mentions amendments to existing orders, route to the **amendment skill**.
* If the user mentions EDI 850 specifically, route to **edi-850-converter**.

---

## 2. Problem Domain
Email-channel order intake involves non-EDI customers (regional distributors, small retailers, etc.) who send purchase orders via various formats.

### Common Friction Points
* **Customer-specific SKUs:** Buyers using their own part numbers.
* **UOM Ambiguity:** Confusion between "pallets," "cases," and "eaches."
* **Unstructured Layouts:** PDFs with cover letters or Excel sheets with merged cells.
* **Multi-purpose Emails:** Messages containing a PO, a ship-to update, and a question.
* **Incomplete Information:** Missing delivery dates, ambiguous ship-to addresses, or no PO number.

---

## 3. Architecture & Pipeline
The skill follows an 8-step pipeline from intent classification to ERP submission.

1. **Policy Snapshot:** Resolve effective validation policy (tenant → customer → doc_type).
2. **Artifact Collection:** Gather email body and all attachments.
3. **Multi-Format Extraction:** Dispatch per source format using template fingerprints and field priors.
4. **Normalization & Merge:** Common schema creation; body overrides attachments on explicit edits.
5. **Entity Resolution:** Map customers, materials, and addresses.
6. **Validation Suite:** Run all checks (CHEAP vs. EXPENSIVE) per snapshotted policy.
7. **ERP Simulation:** Optional dry-run in the ERP to catch errors before final submission.
8. **Submit to ERP:** Final submission gated by a circuit-breaker and idempotency keys.

### Extraction Confidence Scoring
Every field carries a composite confidence score based on:
* `extraction_raw_confidence`
* `resolution_confidence`
* `customer_field_confidence_prior`

**L2 Behavior Thresholds:**
* **≥ 0.95:** Eligible for one-click approve.
* **0.85 - 0.94:** Standard review required.
* **< 0.85:** Low-confidence flag with detailed evidence surfaced.

---

## 4. Resolution Workflows
When failures occur during validation, the system routes them to one of six paths:

| Action | Description |
| :--- | :--- |
| **AUTO_CORRECT** | Agent applies a deterministic fix (requires confidence ≥ 0.99). |
| **AUTO RETRY** | Agent retries via a primitive's fallback chain (e.g., fuzzy SKU match). |
| **AGENT PROPOSES / HUMAN DECIDES** | Candidate fix staged in review UI for one-click approval. |
| **REQUEST CLARIFICATION** | Typed question bundle sent to customer via email. |
| **ESCALATE** | Route to internal finance/pricing/supply teams. |
| **REJECT** | Terminal close for FATAL errors (unauthorized sender, corrupt input). |

### The "Non-Disable-able Floor"
Four critical checks **cannot** be disabled by any configuration:
1. Sender Authorization Check
2. Duplicate PO Check
3. Credit Block Check
4. Customer Identity Resolution

---

## 5. Autonomy Levels
| Level | Behavior | Default Usage |
| :--- | :--- | :--- |
| **L1: Observe** | Human does all review and submission. | New customers (first 30 days). |
| **L2: Recommend** | Agent proposes fixes; human signs off. | Active customers post-onboarding. |
| **L3: Act & Inform** | Agent auto-approves if all signals are green. | Requires graduation gates. |
| **L4: Full Autonomy**| No human touch. | Not recommended at launch. |

---

## 6. Integration & Dependencies
### Primitive Dependencies
* **Required (Cheap):** Sender auth, Customer identity resolver, Material resolver, UOM conversion, MOQ/Overmax check.
* **Required (Expensive):** Document text extractor, Pricing variance check, Credit check, ATP check, Delivery date feasibility.

### MCP Tool Dependencies (ERP Integration)
* `resolve_customer_by_email`
* `resolve_material`
* `get_pricing_for_sku`
* `check_credit_availability`
* `check_atp_availability`
* `simulate_sales_order`
* `create_sales_order`

---

## 7. Metrics & Graduation
### Key Metrics
* **Field extraction accuracy:** Target ≥ 95%.
* **Customer SKU resolution:** Target ≥ 99% (with fuzzy match).
* **Auto-approval rate at L2+:** Target ≥ 60%.
* **ERP submission success rate:** Target ≥ 95%.

### L2 → L3 Graduation Gates
Requires meeting the following over a 90-day window:
* Human edit rate at review: ≤ 5% of fields.
* Downstream ERP rejection rate: ≤ 1%.
* Customer/Invoice dispute rate: ≤ 2% / ≤ 1%.
* Reviewer diversity: ≥ 3 distinct approvers.

---

## 8. Tech Stack
* **Backend:** Node.js + Express; Python (calibration & extraction).
* **Database:** PostgreSQL 15+ (with Row-Level Security).
* **AI/LLM:** Anthropic Claude (reasoning & drafting); Document-intelligence provider.
* **Frontend:** React + ASOE Design System.
* **Integrations:** MCP servers for ERP; Microsoft Graph for Email.
