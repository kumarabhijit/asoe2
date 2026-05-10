"""Spec-as-oracle: every StubGateway response in `api/sandbox_gateways.py`
must round-trip through the `GatewayResponse` Pydantic model and
populate a non-empty `data` dict when the corresponding
GatewayDependency is `required_for_audit`.

A stub whose payload schema drifts from the model gateways return is
the "mock-vs-real" leak class named in `docs/test-strategy/design.md`
P1: tests pass against a stub that real production data would never
match. Catching it at import time (this test runs at collection time)
makes the divergence structurally impossible.

Reference: docs/test-strategy/design.md Lane 1 Week 3 (Schema-validated
stubs); docs/test-strategy/eng-review-test-plan.md (Edge Cases — "Pydantic
model schema drift in StubGateway response → import-time validation
error").
"""

from __future__ import annotations

import os
from typing import Iterator, List, Tuple

import pytest

from api.sandbox_gateways import register_sandbox_gateways
from contracts.models import GatewayResponse
from gateways.registry import (
    _GATEWAY_REGISTRY as _REGISTRY,  # type: ignore[attr-defined]
)
from gateways.stub import StubGateway
from recipes.registry import REGISTRY as RECIPE_REGISTRY


# Module-import-time stub registration: collection-time parametrization
# needs the registry populated before pytest expands the parametrize
# marker. `register_sandbox_gateways()` is a no-op unless ASOE_ENV is
# "sandbox" — but the conftest autouse fixture that pins ASOE_ENV runs
# AFTER collection-time module import, so we must pin it here too.
#
# CRITICAL: do NOT use `os.environ.setdefault` — that leaks the value
# into the rest of the pytest session and changes downstream module
# import behavior (notably `api.deps._resolve_token_ttls`, which reads
# ASOE_ENV at module import to choose 7-day vs 30-day refresh TTLs).
# Save → set → register → restore so the rest of the test session
# sees the original env (or absence thereof) and `tests/conftest.py`
# remains the single fixture that pins ASOE_ENV.
_prev_asoe_env = os.environ.get("ASOE_ENV")
os.environ["ASOE_ENV"] = "sandbox"
try:
    register_sandbox_gateways()
finally:
    if _prev_asoe_env is None:
        os.environ.pop("ASOE_ENV", None)
    else:
        os.environ["ASOE_ENV"] = _prev_asoe_env


def _stub_responses() -> Iterator[Tuple[str, str, GatewayResponse]]:
    """Yield (gateway_name, operation, response) for every stub
    response registered under a StubGateway."""
    for name, gateway in _REGISTRY.items():
        if not isinstance(gateway, StubGateway):
            continue
        # `_responses` is the canonical store on StubGateway; iterate
        # via the protected attr because there is no public accessor.
        for op, resp in gateway._responses.items():  # type: ignore[attr-defined]
            yield name, op, resp


def _required_for_audit_pairs() -> set[Tuple[str, str]]:
    """Set of (gateway_name, operation) tuples declared by any recipe
    as required_for_audit=True. Failure of these gateways halts the
    graph with FAIL_TO_HUMAN, so the stub MUST yield a non-empty
    payload for tests to exercise downstream audit composition."""
    pairs: set[Tuple[str, str]] = set()
    for spec in RECIPE_REGISTRY.values():
        for dep in spec.dependencies:
            if getattr(dep, "required_for_audit", True):
                pairs.add((dep.gateway_name, dep.operation))
    return pairs


_STUB_RESPONSES: List[Tuple[str, str, GatewayResponse]] = list(_stub_responses())
_REQUIRED_PAIRS = _required_for_audit_pairs()


@pytest.mark.parametrize(
    "gateway_name,operation,response",
    _STUB_RESPONSES,
    ids=[f"{n}.{op}" for n, op, _ in _STUB_RESPONSES],
)
def test_stub_response_validates_as_gateway_response(
    gateway_name: str, operation: str, response: GatewayResponse,
) -> None:
    """Round-trip: dump the response and re-validate. Drift between
    what `StubGateway` accepts and what `GatewayResponse` requires
    surfaces here — not in a downstream recipe failure."""
    GatewayResponse.model_validate(response.model_dump())


@pytest.mark.parametrize(
    "gateway_name,operation,response",
    _STUB_RESPONSES,
    ids=[f"{n}.{op}" for n, op, _ in _STUB_RESPONSES],
)
def test_stub_gateway_name_matches_registration(
    gateway_name: str, operation: str, response: GatewayResponse,
) -> None:
    """A stub response carrying gateway_name='oms' registered on the
    'sap_doc' gateway is a copy-paste bug that produces confusing
    trace records and wrong audit attribution."""
    assert response.gateway_name == gateway_name, (
        f"Stub for {gateway_name}.{operation} has gateway_name="
        f"{response.gateway_name!r} on the response — mismatched "
        f"with the registration key {gateway_name!r}."
    )
    assert response.operation == operation, (
        f"Stub for {gateway_name}.{operation} carries operation="
        f"{response.operation!r} on the response — mismatched."
    )


@pytest.mark.parametrize(
    "gateway_name,operation,response",
    _STUB_RESPONSES,
    ids=[f"{n}.{op}" for n, op, _ in _STUB_RESPONSES],
)
def test_required_for_audit_stubs_are_non_empty(
    gateway_name: str, operation: str, response: GatewayResponse,
) -> None:
    """If any recipe declares (gateway, op) as required_for_audit and
    the stub returns SUCCESS with empty data, the audit composer will
    surface the gap — but only at runtime. Catch it here."""
    if (gateway_name, operation) not in _REQUIRED_PAIRS:
        pytest.skip(
            f"({gateway_name}, {operation}) is not required_for_audit "
            f"in any recipe — empty data is acceptable."
        )
    if response.status != "SUCCESS":
        pytest.skip(
            f"({gateway_name}, {operation}) stub status is "
            f"{response.status} — empty data acceptable for non-success."
        )
    assert response.data, (
        f"Stub for {gateway_name}.{operation} is required_for_audit "
        f"but returns SUCCESS with empty data. The audit composer "
        f"will route the record to AUDIT_CONTEXT_MISSING — almost "
        f"certainly a stale stub from a refactor."
    )


def test_every_required_pair_has_a_stub() -> None:
    """Every (gateway, op) declared as required_for_audit by some
    recipe must have a registered gateway entry. Stub-registered
    gateways must declare a response for the operation; non-stub
    (real) gateways are exempt — they implement the operation in
    code, not in canned responses."""
    stub_gateway_names: set[str] = {
        n for n, g in _REGISTRY.items() if isinstance(g, StubGateway)
    }
    registered_pairs: set[Tuple[str, str]] = {
        (n, op) for n, op, _ in _STUB_RESPONSES
    }
    missing = {
        (gw, op) for gw, op in _REQUIRED_PAIRS
        if gw in stub_gateway_names and (gw, op) not in registered_pairs
    }
    # Gateways that are required-for-audit but not registered at all
    # (neither stub nor real) — also a coverage gap.
    unregistered = {
        gw for gw, _ in _REQUIRED_PAIRS if gw not in _REGISTRY
    }
    assert not missing, (
        "The following (gateway, operation) pairs are declared as "
        "required_for_audit by a recipe but the StubGateway has no "
        f"response registered: {sorted(missing)}.\n"
        "Add a `responses={op: GatewayResponse(...)}` entry to the "
        "StubGateway in api/sandbox_gateways.py and tests/conftest.py."
    )
    assert not unregistered, (
        f"Required-for-audit gateways with no registration at all: "
        f"{sorted(unregistered)}. Wire them in api/sandbox_gateways.py."
    )
