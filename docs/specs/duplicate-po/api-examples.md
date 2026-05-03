# API Examples — Duplicate PO Check

> **Status: REFERENCE — preserved from original product spec.**
>
> Per **ADR-028 Guard-rail 2**, V1 implements `GET /api/v1/exceptions/duplicates/:id` as a single canonical envelope endpoint composed in `api/analysis_composer.py`. The examples below show the original spec's intended request/response shapes; the implementation reconciles these against the canonical envelope (ADR-028) and the existing `api/routes/exceptions.py` patterns.
>
> Read this file alongside **ADR-028** (envelope shape) and **ADR-030** (config endpoints).

---

## Inbound PO Request

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

---

## Response — Duplicate Detected (REVIEW_REQUIRED)

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

---

## Response — No Duplicate (PASS)

```json
{
  "order_id": "ord_def456",
  "status": "ACCEPTED",
  "duplicate_check": {
    "classification": "PASS",
    "composite_score": 0.12,
    "matched_order": null,
    "recommended_action": null,
    "autonomy_level": null,
    "evidence_summary": null,
    "signals": {}
  }
}
```

---

## Resolution Request

```
POST /api/v1/exceptions/duplicates/:id/resolve
Content-Type: application/json

{
  "action": "BLOCK_AND_NOTIFY",
  "notes": "Confirmed duplicate — buyer's EDI system retransmitted after portal submission.",
  "notify_buyer": true,
  "notification_template": "duplicate_po_blocked"
}
```

---

## Resolution Response

```json
{
  "id": "dup_check_abc123",
  "resolved_action": "BLOCK_AND_NOTIFY",
  "resolved_by": "user_jdoe",
  "resolved_at": "2026-03-27T14:32:08Z",
  "notification_sent": true,
  "notification_channel": "EDI_997",
  "status": "RESOLVED"
}
```

---

## V1 ASOE Mapping (Implementation Notes)

The shapes above are the **original spec** request/response. ASOE V1 reconciles them as follows:

| Spec endpoint | V1 ASOE endpoint | Notes |
|---|---|---|
| `POST /api/v1/orders/inbound` | Existing `POST /api/v1/exceptions/resolve` (and Phase B bus ingestion per ADR-026) | One canonical ingestion path; per-source connectors translate to `OrderEvent` shape. |
| (Implicit detail fetch in spec) | `GET /api/v1/exceptions/duplicates/:id` (canonical envelope per ADR-028 Guard-rail 2) | Single round trip. Returns `incoming_po`, `matched_po`, `detection`, `human_actions`, `audit_trail`. |
| `POST /api/v1/exceptions/duplicates/:id/resolve` | Existing resolution endpoint with `resolution_reason_tag` from ADR-033 | Reason tag is now a structured `INTENT_REASON_TAGS["DUPLICATE_PO"]` value, not free-text only. |
| (Implicit override-reason vocabulary) | `GET /api/v1/health.allowed_override_reason_tags_by_intent` | UI consumes this for `OverrideChooserDialog` per ADR-033 §D. |
| Config edits (implicit in spec) | `POST /api/v1/config/:intent/:layer` and 4 sibling endpoints (ADR-030 §F) | 5-level override hierarchy with `ConfigChange` audit. |
