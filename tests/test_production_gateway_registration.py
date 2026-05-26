"""PARITY-0 Phase 0b — fail-loud production boot.

Production boot without an explicit real-connector implementation must
**refuse to start**, not silently run with an empty gateway registry where
the first /resolve call fails with KeyError. Per the v3 plan (Security +
Azure/SRE review), the same rule applies to ANY non-sandbox env that
isn't recognised (e.g. a misconfigured `ASOE_ENV=stagin`): refuse to
boot, name the missing wiring.
"""
from __future__ import annotations

import pytest


def test_production_register_raises_not_implemented_today():
    """Until the platform team wires real connectors, the production
    registration function exists and fails loud with a message naming
    where to land them."""
    from api.production_gateways import register_production_gateways
    with pytest.raises(NotImplementedError) as exc_info:
        register_production_gateways()
    msg = str(exc_info.value)
    # The message must be actionable: name the file the next engineer
    # needs to edit.
    assert "production" in msg.lower()
    assert "api/production_gateways.py" in msg or "register_production_gateways" in msg


def test_app_boot_under_production_env_raises(monkeypatch):
    monkeypatch.setenv("ASOE_ENV", "production")
    from api.app import create_app
    with pytest.raises(NotImplementedError):
        create_app()


def test_app_boot_under_unknown_env_refuses(monkeypatch):
    """Any non-sandbox env requires explicit registration. An unknown
    value (typo, misconfigured deploy) must refuse to boot rather than
    silently use an empty registry."""
    monkeypatch.setenv("ASOE_ENV", "staging-mystery")
    from api.app import create_app
    with pytest.raises(RuntimeError) as exc_info:
        create_app()
    msg = str(exc_info.value)
    assert "staging-mystery" in msg or "unknown" in msg.lower()
    assert "sandbox" in msg.lower() and "preprod" in msg.lower()


def test_app_boot_under_sandbox_unchanged(monkeypatch):
    """Backwards-compat: existing sandbox boot path remains."""
    monkeypatch.setenv("ASOE_ENV", "sandbox")
    from gateways import registry
    registry.clear_registry()
    from api.app import create_app
    create_app()
    assert "email_intake" in registry.registered_gateways()
