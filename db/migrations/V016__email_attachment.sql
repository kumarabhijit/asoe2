-- V016 — email_attachment store (DoR #10 — the production attachment store).
--
-- Persists inbound Customer-Inbox email attachments as content in ASOE's own
-- database rather than fetching them from an external service at read time — so
-- attachment retrieval has no outbound-network (SSRF) dependency. The
-- attachment_fetch gateway's SSRF guard remains in place for the case where an
-- attachment is delivered as an external URL that must be retrieved once at
-- ingestion before being stored here.
--
-- Content is stored base64-encoded in a TEXT column for adapter portability
-- (identical binding on SQLite and Postgres; integrity is guaranteed by the
-- stored SHA-256 over the raw bytes). A BYTEA/BLOB column is a future
-- optimisation if storage size becomes a concern. Mirrors the in-memory store
-- in gateways/attachment_store.py.

CREATE TABLE IF NOT EXISTS email_attachment (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    case_id     TEXT,
    name        TEXT NOT NULL,
    mime_type   TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    content_b64 TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Per-case lookup (operator opens a case → list its attachments), tenant-scoped.
CREATE INDEX IF NOT EXISTS idx_email_attachment_case
    ON email_attachment (tenant_id, case_id);

CREATE INDEX IF NOT EXISTS idx_email_attachment_tenant_time
    ON email_attachment (tenant_id, created_at);
