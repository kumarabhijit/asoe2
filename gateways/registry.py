from __future__ import annotations

from typing import Dict, List

from gateways.base import InfrastructureGateway

_GATEWAY_REGISTRY: Dict[str, InfrastructureGateway] = {}


def register_gateway(gateway: InfrastructureGateway) -> None:
    """Register an infrastructure gateway adapter by name."""
    _GATEWAY_REGISTRY[gateway.name] = gateway


def get_gateway(name: str) -> InfrastructureGateway:
    """Look up a registered gateway.  Raises ``KeyError`` if not found."""
    if name not in _GATEWAY_REGISTRY:
        raise KeyError(f"Gateway not registered: {name}")
    return _GATEWAY_REGISTRY[name]


def registered_gateways() -> List[str]:
    """Return all registered gateway names (read-only)."""
    return list(_GATEWAY_REGISTRY.keys())


def clear_registry() -> None:
    """Reset the gateway registry.  Test-only."""
    _GATEWAY_REGISTRY.clear()
