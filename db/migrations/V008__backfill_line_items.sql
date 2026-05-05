-- V008 — Backfill resolution_data.line_items for legacy exception rows.
--
-- The /api/v1/exceptions/{id}/line-items endpoint reads
-- `record.resolution_data.line_items`. PR #105 added a runtime
-- projection in `_persist_exception` so every NEWLY-ingested record
-- carries a single LineItem-shaped row (line_id / sku / uom /
-- quantity / erp_price / po_price) derived from the inbound event.
-- Records persisted BEFORE that PR landed have no line_items key,
-- which is what the operator sees as an empty "Evidence Detail"
-- pane on production.
--
-- This migration backfills `resolution_data.line_items` for those
-- legacy rows by mirroring the runtime projection at the SQL layer.
-- The projection reads `original_event` (a per-record JSONB snapshot
-- of the inbound event captured by V002) and constructs a single
-- LineItem dict — the same shape the runtime path produces for new
-- rows. This keeps the GET /line-items endpoint's response shape
-- uniform across legacy and new records.
--
-- Safety properties:
--
--   * **Idempotent.** The WHERE clause skips any row where
--     `line_items` is already a non-empty array — re-running the
--     migration is a no-op.
--   * **Non-destructive.** Recipes that emit a richer multi-line
--     list still win; the migration ONLY adds a key that's absent
--     and never overwrites recipe-supplied data.
--   * **Tenant-agnostic.** No tenant context required — the
--     projection is per-record and uses fields already on the row.
--   * **V007-compatible.** DUPLICATE_PO records that haven't yet
--     reached a terminal status, OR that already carry the four
--     V007-required `resolution_data` keys, pass the trigger
--     unchanged. The WHERE clause excludes the (rare) pre-V007
--     DUPLICATE_PO rows that lack the audit-bearing keys; those are
--     pre-existing data-quality issues out of scope here.
--   * **Skips rows without a source.** When `original_event` is
--     NULL (an extremely old row from before V002), there is no
--     event metadata to project from and the row is left as-is.

UPDATE exceptions
SET resolution_data = jsonb_set(
    COALESCE(resolution_data, '{}'::jsonb),
    '{line_items}',
    jsonb_build_array(
        jsonb_build_object(
            'line_id',
                COALESCE(original_event->>'order_id', '') || '-' ||
                COALESCE(original_event->>'line_item', '1'),
            'sku',
                COALESCE(
                    NULLIF(original_event->>'sku', ''),
                    original_event->>'order_id',
                    ''
                ),
            'description',
                COALESCE(
                    NULLIF(original_event->>'event_type', ''),
                    'Order line'
                ),
            'uom', 'EA',
            'quantity',
                COALESCE(
                    NULLIF(original_event->>'line_count', '')::int,
                    1
                ),
            'erp_price',
                COALESCE(
                    NULLIF(original_event->>'sap_base_price', '')::float,
                    0.0
                ),
            'po_price',
                COALESCE(
                    NULLIF(original_event->>'po_price', '')::float,
                    0.0
                )
        )
    )
)
WHERE original_event IS NOT NULL
  AND (
      resolution_data IS NULL
      OR NOT (resolution_data ? 'line_items')
      OR jsonb_typeof(resolution_data->'line_items') != 'array'
      OR jsonb_array_length(resolution_data->'line_items') = 0
  )
  -- V007 trigger compatibility: skip pre-V007 DUPLICATE_PO rows
  -- that lack the four audit-bearing keys (signal_breakdown,
  -- composite_score, classification, recommended_action). Updating
  -- those rows would trip the BEFORE INSERT OR UPDATE trigger added
  -- in V007 and abort the entire migration. Such rows are
  -- pre-existing non-compliant data and are out of scope here.
  AND (
      intent != 'DUPLICATE_PO'
      OR final_status IS NULL
      OR (
          resolution_data ? 'signal_breakdown'
          AND resolution_data ? 'composite_score'
          AND resolution_data ? 'classification'
          AND resolution_data ? 'recommended_action'
      )
  );
