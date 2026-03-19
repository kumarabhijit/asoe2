from __future__ import annotations

from typing import Protocol, runtime_checkable

from contracts.models import GatewayRequest, GatewayResponse


@runtime_checkable
class InfrastructureGateway(Protocol):
    """Port interface for infrastructure operations (Hexagonal Architecture).

    Adapters implement this protocol to connect to external systems
    (SAP, databases, APIs).  Recipes never call gateways directly —
    the orchestration layer resolves dependencies before recipe
    execution and applies effects after.

    Test doubles (``StubGateway``) satisfy the same contract, so the
    full graph can run without network access.
    """

    @property
    def name(self) -> str:
        """Unique gateway identifier used in registry lookups."""
        ...

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        """Execute an infrastructure operation and return a typed response."""
        ...

    def health_check(self) -> bool:
        """Return True if the gateway is operational."""
        ...
