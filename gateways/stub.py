from __future__ import annotations

from typing import Dict, List, Optional

from contracts.models import GatewayRequest, GatewayResponse


class StubGateway:
    """Test double for infrastructure gateways.

    Returns canned responses keyed by operation name.  Records every
    call for assertion in tests.  Satisfies ``InfrastructureGateway``
    protocol without network access.
    """

    def __init__(
        self,
        gateway_name: str,
        responses: Optional[Dict[str, GatewayResponse]] = None,
    ) -> None:
        self._name = gateway_name
        self._responses: Dict[str, GatewayResponse] = responses or {}
        self._calls: List[GatewayRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def calls(self) -> List[GatewayRequest]:
        """All requests received (in order)."""
        return list(self._calls)

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        self._calls.append(request)
        if request.operation in self._responses:
            return self._responses[request.operation]
        return GatewayResponse(
            gateway_name=self._name,
            operation=request.operation,
            status="SUCCESS",
            data={},
        )

    def health_check(self) -> bool:
        return True
