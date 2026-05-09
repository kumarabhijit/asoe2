-- ADR-038 Phase H.7 — backfill of orphan OrderCase rows.
--
-- For every exception_record with parent_case_id IS NULL we create
-- an OrderCase and attach the record. The Python in-memory companion
-- (agents/backfill.py::backfill_orphan_cases) implements the same
-- policy; this SQL is the Postgres-side equivalent for the
-- DB-backed deployment path.
--
-- Idempotent: re-running is a no-op because the second run finds
-- no records with NULL parent_case_id.
--
-- Source / source_channel inference here is deliberately simple:
--  * Manual heuristic — event_type LIKE 'EMAIL\_%' → manual_order/email
--  * Default — automated_order/edi_x12_850
-- The Python companion uses the more nuanced derive_source_and_channel
-- with explicit metadata overrides; the SQL path approximates because
-- it can't reach into JSONB metadata without joins. Both paths agree
-- on the policy: Manual when the event-type prefix says so, else
-- Automated.
--
-- Optional Pass 2 (merge by (tenant, customer_po)) is NOT in this
-- migration. It's a separate maintenance script (see
-- agents/backfill.py::merge_orphan_cases_by_correlation). Pass 2
-- requires a maintenance window because it affects existing case
-- references and downstream UI URLs (case_id changes).

BEGIN;

-- 1. Insert one OrderCase per orphan ExceptionRecord. The case_id
--    is derived from the record's tenant + PO so the same migration
--    is replayable without duplicates.

INSERT INTO order_case (
    case_id,
    tenant_id,
    customer_id,
    source,
    source_channel,
    customer_po_number,
    sales_order_id,
    edi_transaction_id,
    source_email_id,
    opened_at,
    status,
    sla_deadline,
    tier,
    bundle_version_at_open
)
SELECT
    -- Derive a deterministic case_id from tenant + order_id so the
    -- migration is replayable. Format: orph-<sha256(tenant||order_id)>[:16].
    substr(encode(sha256((er.tenant_id || ':' || er.order_id)::bytea), 'hex'), 1, 16) AS case_id,
    er.tenant_id,
    NULL                                          AS customer_id,
    CASE
        WHEN er.event_type LIKE 'EMAIL\_%'        THEN 'manual_order'
        ELSE                                           'automated_order'
    END                                           AS source,
    CASE
        WHEN er.event_type LIKE 'EMAIL\_%'        THEN 'email'
        ELSE                                           'edi_x12_850'
    END                                           AS source_channel,
    er.order_id                                   AS customer_po_number,
    NULL                                          AS sales_order_id,
    NULL                                          AS edi_transaction_id,
    NULL                                          AS source_email_id,
    er.created_at                                 AS opened_at,
    -- Existing FAILED records → FAILED case; everything else → OPEN_AGENT_PROCESSING.
    CASE
        WHEN er.lifecycle_state = 'FAILED'        THEN 'FAILED'
        WHEN er.lifecycle_state = 'BLOCKED'       THEN 'BLOCKED'
        WHEN er.lifecycle_state = 'RESOLVED'      THEN 'RESOLVED'
        ELSE                                           'OPEN_AGENT_PROCESSING'
    END                                           AS status,
    -- Default 48h deadline (knowledge/policy/sla_per_customer_tier.yaml
    -- default_sla_hours). Per-tier overrides land in a follow-up
    -- backfill once customer_id ↔ tier_name mapping is materialised.
    (er.created_at::timestamptz + INTERVAL '48 hours')::text AS sla_deadline,
    2                                             AS tier,
    'legacy-pre-h2'                               AS bundle_version_at_open
FROM exception_record er
WHERE er.parent_case_id IS NULL
ON CONFLICT (case_id) DO NOTHING;

-- 2. Register correlation rows so future events on the same PO
--    attach to the orphan case.

INSERT INTO case_correlation_keys (
    tenant_id, key_type, key_value, case_id, registered_at
)
SELECT
    er.tenant_id,
    'customer_po_number'                          AS key_type,
    er.order_id                                   AS key_value,
    substr(encode(sha256((er.tenant_id || ':' || er.order_id)::bytea), 'hex'), 1, 16) AS case_id,
    NOW()::text                                   AS registered_at
FROM exception_record er
WHERE er.parent_case_id IS NULL
ON CONFLICT (tenant_id, key_type, key_value) DO NOTHING;

-- 3. Point the orphan records at their newly-materialised cases.

UPDATE exception_record er
SET    parent_case_id = substr(
            encode(sha256((er.tenant_id || ':' || er.order_id)::bytea), 'hex'),
            1, 16
       )
WHERE  er.parent_case_id IS NULL;

COMMIT;
