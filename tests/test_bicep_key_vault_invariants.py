"""PARITY-4 — bicep-level invariants for Key Vault + separate signing keys.

Source-grep locks (not a deploy test): the bicep file must declare
soft-delete + purge protection + RBAC mode, and must wire the new
``ASOE_ATTACHMENT_SIGNING_KEY`` secretref alongside ``ASOE_JWT_SECRET``.
A deploy that accidentally regresses any of these invariants would be
caught here before it hits an Azure resource group.
"""

from __future__ import annotations

from pathlib import Path

import pytest


BICEP = Path(__file__).resolve().parent.parent / "infra" / "main.bicep"


@pytest.fixture(scope="module")
def bicep_text() -> str:
    return BICEP.read_text()


class TestKeyVaultInvariants:
    def test_key_vault_resource_declared(self, bicep_text: str) -> None:
        assert "Microsoft.KeyVault/vaults@" in bicep_text

    def test_soft_delete_enabled(self, bicep_text: str) -> None:
        assert "enableSoftDelete: true" in bicep_text

    def test_soft_delete_retention_90d(self, bicep_text: str) -> None:
        assert "softDeleteRetentionInDays: 90" in bicep_text

    def test_purge_protection_enabled(self, bicep_text: str) -> None:
        assert "enablePurgeProtection: true" in bicep_text

    def test_rbac_mode_not_access_policies(self, bicep_text: str) -> None:
        assert "enableRbacAuthorization: true" in bicep_text


class TestSeparateSigningKeySecretrefs:
    def test_attachment_signing_key_secret_declared(self, bicep_text: str) -> None:
        assert "asoe-attachment-signing-key" in bicep_text

    def test_attachment_signing_key_envvar_wired(self, bicep_text: str) -> None:
        assert "ASOE_ATTACHMENT_SIGNING_KEY" in bicep_text
        assert "secretRef: 'asoe-attachment-signing-key'" in bicep_text

    def test_attachment_secondary_key_envvar_wired(self, bicep_text: str) -> None:
        """Rotation overlap support — old key moves to secondary for
        the window."""
        assert "ASOE_ATTACHMENT_SIGNING_KEY_SECONDARY" in bicep_text
        assert "secretRef: 'asoe-attachment-signing-key-secondary'" in bicep_text

    def test_jwt_secret_still_wired(self, bicep_text: str) -> None:
        """Regression: do not accidentally drop the JWT secret while
        adding the attachment key."""
        assert "ASOE_JWT_SECRET" in bicep_text
        assert "secretRef: 'asoe-jwt-secret'" in bicep_text
