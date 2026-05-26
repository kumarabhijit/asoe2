# ADR-044: Document Storage & Lifecycle

**Status:** Accepted (non-governance scope) — 2026-05-26. The storage mechanics
ship: object-store backend + filesystem/S3 drivers + env-driven selection,
scoped short-TTL signed reads, DB-backend right-to-erasure delete, and frozen
renditions. **Data-governance items are explicitly deferred** (preprod, far from
GA): retention/TTL policy, encryption-at-rest, routing the erasure tombstone
into the immutable audit chain, and the compliance/CODEOWNERS sign-off gate.
Proposed 2026-05-25.
**Date:** 2026-05-25
**Deciders:** Principal AI/Agentic Engineering Architect; Data Engineering; Compliance Engineer (veto); Security Engineer; Platform/SRE; Product Owner.
**Applies to:**
* asoe2: `gateways/attachment_store.py` (pluggable backend), `db/migrations/` (metadata table, blob-column retirement), `api/routes/attachments.py` (signed-URL / streamed read), `compliance/audit_bearing_registry.yaml`, `contracts/policy.py` (retention/TTL config), a future object-storage backend module.

**Related:**
* ADR-022 (database access pattern), ADR-023 (cosign + hash-chained audit — the immutable chain this must not break).
* ADR-042 (the attachment store + V016 `email_attachment`, PR #169 — current BYTEA-in-primary-DB shape).
* **ADR-043 (Attachment Preview)** — turns attachment bytes into a *hot, operator-facing read path*; this ADR is its **GA precondition**.
* **ADR-045 (Spatial Evidence Extraction)** — produces *frozen renditions* + derived anchors whose lifecycle this ADR governs.
* `docs/plans/attachment-preview-evidence-rollout.md` (execution plan + test-first backlog).

---

## 1. Context

Attachment bytes are stored today as `BYTEA`/`BLOB` **inside the primary OLTP
database** (`V016 email_attachment`). That was a reasonable call for write-once,
read-rarely audit fodder at prototype volume — V016's own header already notes
the intended exit: *"at real volume the blob moves to object storage with this
table holding only metadata + a URI"*, and `attachment_store.py` is already a
**pluggable backend**.

The panel (Data Engineering + Platform/SRE leads) flagged that **ADR-043 changes
the data temperature**: preview makes every operator opening a case pull
multi-MB blobs interactively through the *same* database, connection pool, and
read replicas that run cosign/audit-chain financial writes. Three problems
become load-bearing the moment preview ships:

1. **Scaling / blast radius.** Large `BYTEA` rows bloat base backups, WAL/replica
   streams, and buffer cache, on the one database SOX evidence integrity depends
   on.
2. **Retention & right-to-erasure.** Customer documents carry PII. Erasing a
   document must cascade to bytes + any derived artifacts, while the immutable
   audit chain (ADR-023) must be preserved — a genuine governance conflict with
   no decision recorded today.
3. **Coordinate validity (for ADR-045).** A spatial `bbox` is meaningless without
   the exact render basis it was computed against; re-rendering (PDF.js bump,
   office→PDF conversion) moves pixels.

## 2. Decision

### 2.1 Object storage for bytes; the DB holds metadata + URI

Bytes move to an object store (S3 / GCS / MinIO) behind the **existing pluggable
`attachment_store` backend**. The primary DB keeps only:
`attachment_id`, `case_id`, `tenant_id`, `name`, `mime_type`, `size`,
**`sha256` (content address)**, `storage_uri`, timestamps, retention metadata.

* The migration is a **backend swap**, not a rewrite — guaranteed by a
  **storage-portability contract test** (one suite run against the in-memory
  backend *and* a real object-store backend; identical `put/get/list/clear`
  semantics, content-stripped `list`, sha256 integrity). This test lands in
  **ADR-043 Phase 1** so the swap is a config flip.
* A **DB-bloat tripwire** asserts no *new* blob columns land in the OLTP schema
  after V016.

### 2.2 Access via short-TTL, scoped reads

Preview/download read bytes via **short-TTL, tenant + case-scoped signed URLs**
(or streamed through the existing RBAC-checked endpoint where the store backend
can't sign). Never long-lived or public URLs. Signed URLs are unusable after
expiry and across tenants — both asserted by test. The SSRF-guarded fetch path
(`attachment_fetch.py`) remains the only egress for *fetching* external bytes.

### 2.3 Content addressing & idempotency

`sha256` is the **content address and idempotency key** across the whole pipeline
(ingestion → store → extraction (ADR-045) → composer → preview). Re-processing or
a model upgrade keys on `(sha256, …)`, giving free dedup and an audit fact:
*these artifacts were produced from exactly these bytes*.

### 2.4 Retention, TTL & right-to-erasure (the governance decision)

* **Retention/TTL** is policy-configured (`policy.py`), per tenant, with
  encryption at rest and no client-side byte caching beyond the session.
* **Erasure cascade.** Erasing a document removes: the stored bytes, any
  **frozen renditions** (§2.5), and all **derived `EvidenceAnchor`s**.
* **Audit preservation.** The immutable audit chain (ADR-023) is **not** mutated.
  Erasure replaces PII with a **tombstone**: the chain retains the
  `(attachment_id, sha256, evidence-tuple hash)` and an erasure event, never the
  document content. This resolves the erase-vs-audit conflict explicitly:
  *the proof that a decision was made against content of hash X survives; the
  content itself does not.*

### 2.5 Frozen renditions (binding for ADR-045 geometry)

A spatial anchor (ADR-045) binds to a **frozen, hashed page rendition** — the
exact raster/PDF the coordinates were computed against, plus `dpi` and
`renderer_version`. The rendition is stored in object storage and hashed. A
**coordinate-validity invariant** holds: re-rendering the frozen rendition
reproduces the same geometry; a renderer/dpi change without a new rendition hash
is a hard failure. Without this, ADR-045's "overlays that survive re-render" is
false.

## 3. Compliance & security posture

* Object-store buckets are tenant-scoped, encrypted at rest, private (no public
  ACLs), access-logged.
* The erasure tombstone shape is registered in `audit_bearing_registry.yaml`;
  changing it is a compliance CODEOWNERS gate.
* Retention/TTL defaults require compliance sign-off (PII).

## 4. Alternatives considered (rejected)

* **Keep blobs in the primary DB at GA** — rejected: turns a "view PO" feature
  into backup/replica/buffer-cache pressure on the financial-write DB; makes
  clean erasure and TTL hard.
* **Base64 text column** — rejected (already rejected in V016): ~33% bloat, worse
  TOAST behaviour.
* **Hard-delete rows on erasure** — rejected: breaks the ADR-023 hash chain. Use
  the tombstone (§2.4).
* **Long-lived/public object URLs** — rejected: IDOR / data-leak surface.

## 5. Consequences

* The attachment store becomes a thin metadata table + an object-storage backend;
  preview's hot read path leaves the OLTP DB.
* `sha256` becomes the spine for dedup, idempotency, and audit binding.
* A clean, testable erasure story exists for customer PII without breaking audit.
* Frozen renditions add a storage artifact but make ADR-045 geometry verifiable.

## 6. Definition of Done

Object-storage backend behind `attachment_store`; metadata-only OLTP table +
blob-column retirement migration; storage-portability contract test green across
backends; short-TTL scoped signed-URL/streamed read with expiry + cross-tenant
abuse-case tests; retention/TTL policy in `policy.py`; encryption-at-rest;
erasure-cascade test (bytes + renditions + anchors removed, audit chain + tombstone
preserved); frozen-rendition store + coordinate-validity test; registry rows +
compliance sign-off; docs updated.

**This ADR is a GA precondition for ADR-043's preview.** Phase-1 preview may ship
in dev/low-volume on the in-DB backend *only* with the storage-portability
contract test already green, so GA is a config flip.

## 7. Implementation status (CP-E)

Landed in `gateways/attachment_store.py` and locked by green contract tests:

* **`ObjectStoreBackend`** — metadata in an index, bytes behind a pluggable
  `_BlobStore` driver; `get` reconstitutes metadata + blob, `list_for_case` is
  metadata-only, reads are tenant-scoped. `for_testing()` wires an in-process
  driver so the **portability contract** (`test_attachment_store_portability.py`)
  runs without external infra and proves the swap is config-only.
* **Right-to-erasure** — `erase_attachment` removes bytes + metadata and writes a
  **PII-free tombstone** (identity + `sha256`, never content or filename);
  `get_erasure_tombstone` reads it (`test_attachment_erasure_cascade.py`).

**Still open (this ADR's remaining DoD):** a real S3/GCS/MinIO `_BlobStore`
driver + env-driven backend selection; short-TTL **signed URLs**; retention/TTL
policy + encryption-at-rest; routing the erasure tombstone into the **audit
chain** (ADR-023) rather than the in-process registry; and **frozen renditions**
for ADR-045 geometry. These need real infra / compliance sign-off and are the
GA-gating follow-ups.

## 8. Implementation status (CP-G — Accepted, non-governance scope, 2026-05-26)

The storage mechanics are landed and green:

* **Object-store backend + drivers** — `_FilesystemBlobStore` (infra-free,
  path-traversal-guarded, atomic writes) and an env-flagged real S3/MinIO driver
  (`_s3_blob_store`, boto3 lazy-imported, live path only) behind the existing
  `ObjectStoreBackend`/`_BlobStore` seam; env-driven selection via
  `ASOE_ATTACHMENT_BACKEND` / `ASOE_OBJECT_STORE_DRIVER`. The
  storage-portability contract test now runs across the filesystem driver too —
  the GA swap is a config flip.
* **Scoped short-TTL read** — `api/attachment_read_token.py` mints an HMAC
  capability token bound to one `(tenant, case, attachment)` tuple; the
  RBAC-checked mint endpoint + the token-validated `GET /attachments/read` stream
  bytes. Unusable after expiry, cannot cross tenants/cases (both tested).
* **DB right-to-erasure** — `AttachmentRepository.delete` (tenant-scoped) makes
  `erase_attachment` work on the DB backend; bytes removed, PII-free tombstone
  retained, audit chain untouched.
* **Frozen renditions** — `gateways/frozen_rendition.py` freezes a page raster +
  dpi + renderer_version into a content+basis hash stored in the object store,
  with the coordinate-validity invariant (`verify_render_basis` raises on a moved
  basis). `EvidenceAnchor.rendition_hash` binds spatial geometry to it.

**Deferred (governance, out of scope this engagement):** retention/TTL,
encryption-at-rest, audit-chain tombstone routing, compliance/CODEOWNERS gate.
These remain GA preconditions and are tracked, not built here.

---

*End of ADR-044.*
