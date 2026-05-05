-- V008 — Backfill resolution_data.line_items for legacy exception rows.
--
-- The /api/v1/exceptions/{id}/line-items endpoint reads
-- `record.resolution_data.line_items`. PR #105 added a runtime
-- projection in `_persist_exception` so every NEWLY-ingested record
-- carries a list of LineItem-shaped rows derived from the inbound
-- event. Records persisted BEFORE that PR landed have no line_items
-- key, which is what the operator sees as an empty "Evidence
-- Detail" pane on production.
--
-- This migration backfills `resolution_data.line_items` for those
-- legacy rows. The projection runs in two modes (mirroring
-- `api/routes/exceptions.py::_project_line_items`):
--
--   1. **Multi-line** — when `original_event.metadata.line_items` is
--      a non-empty array, expand it into one LineItem per entry.
--      Each entry can override sku / description / uom / quantity /
--      erp_price / po_price / root_cause; fields it omits fall
--      back to the order-level values from the event.
--
--   2. **Single-line fallback** — when no multi-line metadata is
--      present, emit one LineItem from the event's order-level
--      fields (the V1 per-line event shape).
--
-- Both modes are pure projection over fields that already exist on
-- the row — never invents new prices, quantities, or skus.
--
-- Safety properties:
--
--   * **Idempotent.** WHERE clause skips rows where line_items is
--     already a non-empty array. Re-runs are no-ops.
--   * **Non-destructive.** Recipes that emit a richer multi-line
--     list still win; the migration ONLY adds a key that's absent.
--   * **Tenant-agnostic.** No tenant context required.
--   * **V007-compatible.** DUPLICATE_PO records that haven't reached
--     a terminal status, OR that already carry the four
--     V007-required `resolution_data` keys, pass the trigger
--     unchanged. The WHERE clause excludes pre-V007 DUPLICATE_PO
--     rows that lack the audit-bearing keys; those are pre-existing
--     non-compliant data, out of scope here.
--   * **Skips rows without a source.** When `original_event` is NULL
--     the row is left as-is.

WITH legacy AS (
    SELECT
        id,
        original_event,
        resolution_data
    FROM exceptions
    WHERE original_event IS NOT NULL
      AND (
          resolution_data IS NULL
          OR NOT (resolution_data ? 'line_items')
          OR jsonb_typeof(resolution_data->'line_items') != 'array'
          OR jsonb_array_length(resolution_data->'line_items') = 0
      )
      -- V007 trigger compatibility — see header comment.
      AND (
          intent != 'DUPLICATE_PO'
          OR final_status IS NULL
          OR (
              resolution_data ? 'signal_breakdown'
              AND resolution_data ? 'composite_score'
              AND resolution_data ? 'classification'
              AND resolution_data ? 'recommended_action'
          )
      )
),
-- Order-level fallback values (used by both projection modes).
fallbacks AS (
    SELECT
        id,
        original_event,
        resolution_data,
        COALESCE(original_event->>'order_id', '') AS order_id,
        COALESCE(NULLIF(original_event->>'sku', ''),
                 original_event->>'order_id', '') AS fallback_sku,
        COALESCE(NULLIF(original_event->>'event_type', ''),
                 'Order line') AS fallback_desc,
        COALESCE(NULLIF(original_event->>'line_count', '')::int, 1) AS fallback_qty,
        COALESCE(NULLIF(original_event->>'sap_base_price', '')::float, 0.0) AS fallback_erp,
        COALESCE(NULLIF(original_event->>'po_price', '')::float, 0.0) AS fallback_po,
        COALESCE(NULLIF(original_event->>'line_item', '')::int, 1) AS event_line_item,
        original_event->'metadata'->'line_items' AS raw_lines
    FROM legacy
),
-- Multi-line mode: one LineItem per metadata.line_items entry.
multi_line AS (
    SELECT
        f.id,
        jsonb_agg(
            jsonb_build_object(
                'line_id',
                    COALESCE(
                        NULLIF(elem->>'line_id', ''),
                        f.order_id || '-' || COALESCE(elem->>'line_item', ord::text)
                    ),
                'sku',
                    COALESCE(NULLIF(elem->>'sku', ''), f.fallback_sku),
                'description',
                    COALESCE(NULLIF(elem->>'description', ''), f.fallback_desc),
                'uom',
                    COALESCE(NULLIF(elem->>'uom', ''), 'EA'),
                'quantity',
                    COALESCE(NULLIF(elem->>'quantity', '')::int, f.fallback_qty),
                'erp_price',
                    COALESCE(NULLIF(elem->>'erp_price', '')::float, f.fallback_erp),
                'po_price',
                    COALESCE(NULLIF(elem->>'po_price', '')::float, f.fallback_po),
                'root_cause', elem->>'root_cause'
            )
            ORDER BY ord
        ) AS items
    FROM fallbacks f,
         LATERAL jsonb_array_elements(
             CASE WHEN jsonb_typeof(f.raw_lines) = 'array' AND
                       jsonb_array_length(f.raw_lines) > 0
                  THEN f.raw_lines
                  ELSE '[]'::jsonb
             END
         ) WITH ORDINALITY AS t(elem, ord)
    GROUP BY f.id
),
-- Single-line fallback for rows without multi-line metadata.
single_line AS (
    SELECT
        f.id,
        jsonb_build_array(
            jsonb_build_object(
                'line_id', f.order_id || '-' || f.event_line_item,
                'sku', f.fallback_sku,
                'description', f.fallback_desc,
                'uom', 'EA',
                'quantity', f.fallback_qty,
                'erp_price', f.fallback_erp,
                'po_price', f.fallback_po
            )
        ) AS items
    FROM fallbacks f
    WHERE NOT (
        jsonb_typeof(f.raw_lines) = 'array'
        AND jsonb_array_length(f.raw_lines) > 0
    )
),
-- Coalesce: prefer multi_line when present, else single_line.
projected AS (
    SELECT id, items FROM multi_line
    UNION ALL
    SELECT id, items FROM single_line
)
UPDATE exceptions e
SET resolution_data = jsonb_set(
    COALESCE(e.resolution_data, '{}'::jsonb),
    '{line_items}',
    p.items
)
FROM projected p
WHERE e.id = p.id;
