-- V016 — email_attachment store (DoR #10 — the production attachment store).
--
-- Persists inbound Customer-Inbox email attachments as content in ASOE's own
-- database rather than fetching them from an external service at read time — so
-- attachment retrieval has no outbound-network (SSRF) dependency. The
-- attachment_fetch gateway's SSRF guard remains in place for the case where an
-- attachment is delivered as an external URL that must be retrieved once at
-- ingestion before being stored here.
--
-- Content is stored as raw bytes in a BYTEA column (no base64 inflation;
-- integrity is the stored SHA-256 over those bytes). Reads of the attachment
-- list never SELECT the `content` column — only the single-attachment fetch
-- does — so listing a case's attachments doesn't drag blobs. Mirrors the
-- in-memory store in gateways/attachment_store.py. (At real volume the blob
-- moves to object storage with this table holding only metadata + a URI; the
-- SSRF-guarded fetch path we kept is exactly that mode.)

CREATE TABLE IF NOT EXISTS email_attachment (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    case_id     TEXT,
    name        TEXT NOT NULL,
    mime_type   TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    content     BYTEA NOT NULL,
    created_at  TEXT NOT NULL
);

-- Per-case lookup (operator opens a case → list its attachments), tenant-scoped.
CREATE INDEX IF NOT EXISTS idx_email_attachment_case
    ON email_attachment (tenant_id, case_id);

CREATE INDEX IF NOT EXISTS idx_email_attachment_tenant_time
    ON email_attachment (tenant_id, created_at);
