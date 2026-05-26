"""PARITY-8 — data governance closure.

Compliance review CRITICAL requirements:

  * **DPIA gate per tenant**: a per-tenant DPIA record under
    ``compliance/dpia/{tenant-id}.md`` must exist before retention
    enables on that tenant's real data.
  * **Per-tenant residency check**: the retention sweeper refuses to
    delete from a region that violates a tenant's residency
    commitment (declared in ``contracts/policy.py`` per-tenant config).
    Fail loud, log to audit, alert the operator.
  * **Retention-sweeper kill-switch**: ``RETENTION_SWEEPER_ENABLED``
    env var; dry-run produces an audit-logged plan; operator must
    confirm before the sweeper commits. Default DISABLED in preprod.
  * **Distinct audit event type**: ``SCHEDULED_RETENTION_DELETE``,
    distinguishable from operator-triggered erasures in the audit log.
  * **Identity resolution order**: JWT sub → Entra lookup →
    ``system:service-principal`` (for the sweeper).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_lifespan(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


class TestRetentionSweeperKillSwitch:
    def test_sweeper_disabled_by_default_in_preprod(self, monkeypatch):
        from api.retention_sweeper import is_enabled

        monkeypatch.delenv("RETENTION_SWEEPER_ENABLED", raising=False)
        assert is_enabled() is False, (
            "default-disabled is the compliance contract — operator must "
            "explicitly opt in per Decision Q4-equivalent"
        )

    def test_sweeper_opt_in_via_env(self, monkeypatch):
        from api.retention_sweeper import is_enabled

        monkeypatch.setenv("RETENTION_SWEEPER_ENABLED", "true")
        assert is_enabled() is True

    def test_dry_run_produces_plan_without_deleting(self, monkeypatch):
        """A dry-run MUST NOT delete anything; it returns a plan the
        operator confirms before the sweeper commits."""
        from api.retention_sweeper import RetentionSweeper

        sweeper = RetentionSweeper()
        plan = sweeper.dry_run(tenant_id="t-1", as_of_unix=1_700_000_000)
        # Plan shape: a list of candidate items with reason + ttl,
        # NOT the deletion result.
        assert hasattr(plan, "candidates")
        # Plan IS NOT a deletion confirmation.
        assert getattr(plan, "committed", None) is False


class TestResidencyCheck:
    def test_sweeper_refuses_residency_violation(self, monkeypatch):
        """A tenant with EU residency MUST NOT have its data deleted
        from a US storage region (the bytes shouldn't be there in the
        first place; this is the defence-in-depth check)."""
        monkeypatch.setenv("RETENTION_SWEEPER_ENABLED", "true")
        from api.retention_sweeper import RetentionSweeper
        from api.errors import ASOEError

        sweeper = RetentionSweeper()
        with pytest.raises(ASOEError) as exc:
            sweeper.commit_with_residency_check(
                tenant_id="tenant-eu-strict",
                tenant_residency_region="eu-west-1",
                target_storage_region="us-east-2",
                items=[],
            )
        assert exc.value.status_code in (400, 403, 422)

    def test_sweeper_accepts_matching_residency(self, monkeypatch):
        # Sweeper is disabled by default; flipping the kill-switch is
        # part of the operator opt-in.
        monkeypatch.setenv("RETENTION_SWEEPER_ENABLED", "true")
        from api.retention_sweeper import RetentionSweeper

        sweeper = RetentionSweeper()
        result = sweeper.commit_with_residency_check(
            tenant_id="tenant-eu",
            tenant_residency_region="eu-west-1",
            target_storage_region="eu-west-1",
            items=[],
        )
        assert result.residency_ok is True


class TestScheduledDeleteAuditEvent:
    def test_scheduled_delete_event_type_distinct(self):
        """Bulk sweeper deletes must surface as a distinct event type
        so the audit trail distinguishes them from
        operator-triggered ATTACHMENT_ERASED events."""
        from api.retention_sweeper import SCHEDULED_RETENTION_DELETE_EVENT

        assert SCHEDULED_RETENTION_DELETE_EVENT == "SCHEDULED_RETENTION_DELETE"


class TestIdentityResolution:
    def test_resolution_order_jwt_first(self):
        """JWT sub → Entra lookup → system:service-principal."""
        from api.retention_sweeper import resolve_identity_for_sweep

        # Caller supplied a JWT sub.
        identity = resolve_identity_for_sweep(
            jwt_sub="user-123",
            entra_oid=None,
            is_service_principal=False,
        )
        assert identity == "user-123"

    def test_resolution_falls_back_to_entra(self):
        from api.retention_sweeper import resolve_identity_for_sweep

        identity = resolve_identity_for_sweep(
            jwt_sub=None,
            entra_oid="entra-oid-abc",
            is_service_principal=False,
        )
        assert identity == "entra-oid-abc"

    def test_resolution_falls_back_to_service_principal(self):
        from api.retention_sweeper import resolve_identity_for_sweep

        identity = resolve_identity_for_sweep(
            jwt_sub=None, entra_oid=None, is_service_principal=True,
        )
        assert identity == "system:service-principal"

    def test_resolution_raises_with_no_inputs(self):
        from api.retention_sweeper import resolve_identity_for_sweep

        with pytest.raises(ValueError):
            resolve_identity_for_sweep(
                jwt_sub=None, entra_oid=None, is_service_principal=False,
            )


class TestPerTenantTTL:
    def test_default_ttl_resolves(self):
        from contracts.policy import RETENTION_TTL_DAYS_DEFAULT

        assert isinstance(RETENTION_TTL_DAYS_DEFAULT, int)
        assert RETENTION_TTL_DAYS_DEFAULT > 0

    def test_per_tenant_override_resolves(self):
        from contracts.policy import RETENTION_TTL_DAYS_DEFAULT, get_tenant_retention_ttl_days

        # Default tenant uses the default ttl.
        assert get_tenant_retention_ttl_days("any-tenant") == RETENTION_TTL_DAYS_DEFAULT


class TestDpiaGate:
    def test_dpia_template_exists(self):
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "compliance" / "dpia" / "_template.md"
        assert path.exists(), "DPIA template must exist for per-tenant copies"


class TestErasureFlowsDoc:
    def test_erasure_flows_doc_distinguishes_two_modes(self):
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "docs" / "ops" / "erasure-flows.md"
        text = path.read_text()
        assert "erase-customer-data" in text.lower() or "erase customer data" in text.lower()
        assert "erase-content" in text.lower() or "erase content" in text.lower()
        assert "attestation" in text.lower()
