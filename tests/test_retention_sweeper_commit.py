"""PARITY-8 — retention sweeper real delete implementation.

Today ``commit_with_residency_check`` validates residency + opt-in but
returns ``deleted_count=0`` without wiring the actual byte wipe. This
suite locks the real-delete contract:

  * Each candidate flows through ``erase_attachment`` so the
    proof-of-erasure invariant from PARITY-0.5 (audit row BEFORE byte
    wipe) holds for bulk sweeper deletes too.
  * The bulk audit event type is ``SCHEDULED_RETENTION_DELETE`` (not
    ``ATTACHMENT_ERASED``) so the audit trail differentiates
    operator-triggered erasures from sweeper deletes.
  * ``deleted_count`` reflects what actually got wiped; absent
    attachments don't count as deletes and don't write a spurious
    audit row.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


@pytest.fixture
def _clean_audit(monkeypatch):
    from api.store import exception_store
    exception_store.clear()
    from gateways import attachment_store
    attachment_store.reset_erasure_tombstones()
    yield
    exception_store.clear()
    attachment_store.reset_erasure_tombstones()


def _seed_attachments(backend, tenant_id: str, count: int):
    """Drop ``count`` attachments into the backend, return the records."""
    from gateways.attachment_store import store_attachment, configure_backend
    configure_backend(backend)
    records = []
    for i in range(count):
        rec = store_attachment(
            tenant_id=tenant_id, name=f"po-{i}.pdf",
            mime_type="application/pdf", content=f"pdf-body-{i}".encode(),
            case_id=f"case-{i}",
        )
        records.append(rec)
    return records


class TestRealDelete:
    def test_commit_wipes_each_candidate_via_erase(
        self, monkeypatch, _clean_audit,
    ):
        from gateways.attachment_store import _InMemoryBackend, get_attachment
        from api.retention_sweeper import (
            RetentionSweeper, SweepCandidate,
        )

        backend = _InMemoryBackend()
        records = _seed_attachments(backend, tenant_id="tenant-eu", count=3)
        candidates = [
            SweepCandidate(
                attachment_id=r.id, tenant_id=r.tenant_id,
                sha256=r.sha256, ttl_days_applied=365,
                reason="RETENTION_TTL_EXPIRED",
            )
            for r in records
        ]

        monkeypatch.setenv("RETENTION_SWEEPER_ENABLED", "true")
        sweeper = RetentionSweeper()
        result = sweeper.commit_with_residency_check(
            tenant_id="tenant-eu",
            tenant_residency_region="eu-west-1",
            target_storage_region="eu-west-1",
            items=candidates,
        )
        assert result.deleted_count == 3, (
            "every candidate must actually wipe — today's stub returns 0"
        )
        for r in records:
            assert get_attachment(r.tenant_id, r.id) is None, (
                "bytes still present after commit — real delete not wired"
            )

    def test_commit_writes_scheduled_retention_delete_audit_event(
        self, monkeypatch, _clean_audit,
    ):
        """Bulk deletes carry the distinct
        ``SCHEDULED_RETENTION_DELETE`` event type so the audit log can
        distinguish them from operator-triggered ``ATTACHMENT_ERASED``."""
        from gateways.attachment_store import _InMemoryBackend
        from api.retention_sweeper import (
            RetentionSweeper, SweepCandidate,
            SCHEDULED_RETENTION_DELETE_EVENT,
        )
        from api.store import exception_store

        backend = _InMemoryBackend()
        records = _seed_attachments(backend, tenant_id="tenant-eu", count=2)
        candidates = [
            SweepCandidate(
                attachment_id=r.id, tenant_id=r.tenant_id, sha256=r.sha256,
                ttl_days_applied=365, reason="RETENTION_TTL_EXPIRED",
            )
            for r in records
        ]
        monkeypatch.setenv("RETENTION_SWEEPER_ENABLED", "true")
        RetentionSweeper().commit_with_residency_check(
            tenant_id="tenant-eu",
            tenant_residency_region="eu-west-1",
            target_storage_region="eu-west-1",
            items=candidates,
        )

        log = exception_store.get_audit_log("tenant-eu")
        sweeper_events = [
            e for e in log if e.get("policy_key") == SCHEDULED_RETENTION_DELETE_EVENT
        ]
        # One per candidate. The byte-wipe event itself (per PARITY-0.5)
        # also fires under ATTACHMENT_ERASED; both writes are
        # intentional — the SCHEDULED_RETENTION_DELETE row attributes
        # the wipe to the sweeper, the ATTACHMENT_ERASED row carries
        # the PII-free tombstone for the regulator-facing certificate.
        assert len(sweeper_events) == 2

    def test_absent_attachment_is_not_counted_or_audited(
        self, monkeypatch, _clean_audit,
    ):
        """A candidate whose id no longer resolves (already-erased by
        an operator, race with another sweeper) must not increment
        ``deleted_count`` and must not write a spurious audit row."""
        from gateways.attachment_store import _InMemoryBackend
        from api.retention_sweeper import (
            RetentionSweeper, SweepCandidate,
            SCHEDULED_RETENTION_DELETE_EVENT,
        )
        from api.store import exception_store
        from gateways.attachment_store import configure_backend

        backend = _InMemoryBackend()
        configure_backend(backend)
        # Candidate for an attachment id that doesn't exist.
        candidates = [
            SweepCandidate(
                attachment_id="never-existed",
                tenant_id="tenant-eu",
                sha256="0" * 64,
                ttl_days_applied=365,
                reason="RETENTION_TTL_EXPIRED",
            ),
        ]
        monkeypatch.setenv("RETENTION_SWEEPER_ENABLED", "true")
        result = RetentionSweeper().commit_with_residency_check(
            tenant_id="tenant-eu",
            tenant_residency_region="eu-west-1",
            target_storage_region="eu-west-1",
            items=candidates,
        )
        assert result.deleted_count == 0
        log = exception_store.get_audit_log("tenant-eu")
        sweeper_events = [
            e for e in log if e.get("policy_key") == SCHEDULED_RETENTION_DELETE_EVENT
        ]
        assert sweeper_events == []


class TestResidencyOrderingPreserved:
    def test_residency_fails_before_attempting_wipe(
        self, monkeypatch, _clean_audit,
    ):
        """Regression guard: the residency check must STILL fire first
        (per the existing test_retention_governance regression and the
        KNOWN TRAP in the parity prompt) — wiring the real delete must
        not reorder the blast-radius hierarchy."""
        from gateways.attachment_store import _InMemoryBackend, get_attachment
        from api.retention_sweeper import RetentionSweeper, SweepCandidate
        from api.errors import ASOEError

        backend = _InMemoryBackend()
        records = _seed_attachments(backend, tenant_id="tenant-eu", count=1)
        candidates = [
            SweepCandidate(
                attachment_id=records[0].id,
                tenant_id="tenant-eu",
                sha256=records[0].sha256, ttl_days_applied=365,
                reason="RETENTION_TTL_EXPIRED",
            ),
        ]
        monkeypatch.setenv("RETENTION_SWEEPER_ENABLED", "true")
        with pytest.raises(ASOEError) as exc:
            RetentionSweeper().commit_with_residency_check(
                tenant_id="tenant-eu",
                tenant_residency_region="eu-west-1",
                target_storage_region="us-east-2",  # WRONG region
                items=candidates,
            )
        assert exc.value.code == "RESIDENCY_VIOLATION"
        # Bytes must still be in place — the residency refusal preceded
        # any wipe attempt.
        assert get_attachment("tenant-eu", records[0].id) is not None
