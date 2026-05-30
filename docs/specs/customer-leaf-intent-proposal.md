# Customer Leaf-Intent Proposal — ADR-036 Phase 2 (Phase-0 stand-in)

**Status:** Accepted — ratified by PO 2026-05-30 (full set + inquiry leaves)
**Date:** 2026-05-30
**Author:** AI/Agentic Engineering (drafting the Phase-0 data-mining stand-in)
**Ratifier (PO):** secondoption15@gmail.com — option "Approve + add inquiry leaves"
**Authority:** ADR-036 D2b; `case-intent-supergroup-requirements.md` §6.4 ("the final leaf list per super-group is produced by Phase 0; PO approves the additions in a 30-minute review"), §8.3 (reclassification rights).

---

## What this is (and is not)

ADR-036 deliberately did **not** invent customer leaf intents — that is steward/PO
territory. This document is the **proposal** the PO ratifies. In a real Phase-0 sprint
the leaf list comes from a 90-day mining of inbound customer email/case bodies; absent
that corpus, this is a **principled draft derived from each supergroup's own description**
in `db/seeds/case_taxonomy.yaml`. Every proposed leaf is marked `phase_zero_pending: true`
(provisional) until a real corpus confirms frequency.

**These leaves carry no recipe and are not added to `AllowedIntent`.** Per ADR-036 D1,
the email classifier assigns the **supergroup** at intake (`intent_code = NULL`); these
leaves exist so a human (or a later finer classifier) can **reclassify** a case to a
specific leaf (§8.3) and so steward dashboards have leaf-level granularity. Routing
remains on `intent_code` only where an actionable recipe exists — which, for the customer
origin, is a separate future decision per leaf.

## One category stays **supergroup-only** (no leaves)

- **`SG_NEEDS_TRIAGE`** — already supergroup-only with the `INT_UNKNOWN` sentinel. Unchanged.

**PO ratification note (2026-05-30):** the PO elected to add reporting-granularity leaves
to `SG_ORDER_STATUS_INQUIRY` (it is *not* supergroup-only in the final set). See its table below.

## Ratified leaves (11 supergroups → 36 leaves)

Naming obeys §9.2 (no `SG_`/`INT_` suffix collision). All `sap_block_code: null`,
`sap_block_field: null`, `phase_zero_pending: true`.

### `SG_ORDER_STATUS_INQUIRY` — where-is-my-order (PO-added reporting granularity)
| Leaf | Description |
|------|-------------|
| `INT_ORDER_TRACKING` | Tracking / shipment-location request. |
| `INT_ORDER_ETA` | Delivery ETA / date question. |
| `INT_DELIVERY_CONFIRMATION` | Order receipt / delivery-confirmation question. |

### `SG_ORDER_CHANGE` — modify an existing order
| Leaf | Description |
|------|-------------|
| `INT_QTY_CHANGE` | Change to ordered quantity on an existing line. |
| `INT_DATE_CHANGE` | Change to requested delivery / ship date. |
| `INT_SKU_CHANGE` | Add, remove, or substitute a SKU on an existing order. |
| `INT_ORDER_CANCEL` | Cancel an existing order or order line. |

### `SG_SHIPMENT_DISCREPANCY` — what arrived ≠ what was ordered/shipped
| Leaf | Description |
|------|-------------|
| `INT_SHORT_SHIP` | Fewer units received than ordered / shipped. |
| `INT_OVER_SHIP` | More units received than ordered. |
| `INT_DAMAGED_TRANSIT` | Goods damaged in transit. |
| `INT_MISSING_SHIPMENT` | Shipment or POD not received / not located. |

### `SG_RETURN_RGA` — returns & recalls
| Leaf | Description |
|------|-------------|
| `INT_RETURN_REQUEST` | Customer requests authorisation to return goods. |
| `INT_RGA_STATUS` | Status / follow-up on an existing RGA / RMA. |
| `INT_RECALL_RECONCILIATION` | Reconcile returned quantities against a recall. |

### `SG_LOGISTICS_CHANGE` — delivery logistics on an existing order
| Leaf | Description |
|------|-------------|
| `INT_SHIPTO_CHANGE` | Ship-to address change. |
| `INT_ROUTING_CHANGE` | Routing / lane change request. |
| `INT_DELIVERY_WINDOW_CHANGE` | Delivery-window / appointment change. |
| `INT_CARRIER_CHANGE` | Carrier change request. |

### `SG_BILLING_DISPUTE` — money disputes
| Leaf | Description |
|------|-------------|
| `INT_INVOICE_DISPUTE` | Invoice amount / line dispute. |
| `INT_PAYMENT_TERMS_DISPUTE` | Payment-terms disagreement. |
| `INT_FREIGHT_DISPUTE` | Freight / accessorial charge dispute. |
| `INT_DEDUCTION_RECONCILIATION` | Reconcile a customer deduction / chargeback. |

### `SG_DOCUMENTATION` — paperwork requests
| Leaf | Description |
|------|-------------|
| `INT_COA_REQUEST` | Certificate of Analysis request. |
| `INT_MSDS_REQUEST` | MSDS / SDS request. |
| `INT_TAX_CERT_REQUEST` | Tax / exemption certificate request. |
| `INT_CUSTOMS_DOC_REQUEST` | Customs / export paperwork request. |
| `INT_SAMPLE_REQUEST` | Product sample request. |

### `SG_COMPLAINT_SERVICE` — service-level complaints
| Leaf | Description |
|------|-------------|
| `INT_SERVICE_RESPONSIVENESS` | Response-time / SLA complaint. |
| `INT_SERVICE_MISINFORMATION` | Wrong information given by service. |
| `INT_SERVICE_MISSED_FOLLOWUP` | Missed callback / follow-up. |

### `SG_COMPLAINT_PRODUCT` — product-quality complaints
| Leaf | Description |
|------|-------------|
| `INT_PRODUCT_QUALITY` | Product-quality defect complaint. |
| `INT_FOOD_SAFETY_REPORT` | Food-safety report (regulatory-sensitive). |
| `INT_RECALL_TRIGGER` | Complaint that may trigger a recall. |

### `SG_EDI_ESCALATION` — customer-reported EDI failures
| Leaf | Description |
|------|-------------|
| `INT_EDI_850_FAILURE` | Inbound 850 (PO) failure reported by customer. |
| `INT_EDI_855_FAILURE` | 855 (PO ack) failure reported by customer. |
| `INT_EDI_810_FAILURE` | 810 (invoice) failure reported by customer. |

### `SG_NEW_ORDER` — unchanged
Keeps its single existing leaf `INT_MANUAL_ORDER_INTAKE` (already actionable via the intake path).

---

## How it lands (on ratification)

1. Each leaf added via the governed CLI: `python -m scripts.steward_change add-intent --code … --supergroup … --description … --phase-zero-pending --display-name …` (writes YAML, validates §9.2 + invariants, regenerates `taxonomy_constants.py` + `taxonomy.ts`).
2. Bump the seed `version`.
3. `tests/test_taxonomy_constants_drift.py` confirms the committed constants match the YAML.
4. No recipe wiring, no `AllowedIntent` change — these are reclassification/reporting leaves.
