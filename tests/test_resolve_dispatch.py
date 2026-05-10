"""ADR-038 Phase H.5 — `/api/v1/exceptions/resolve` dispatch tests.

Locks the routing-predicate dispatch:
  * Default (no env var) → deterministic graph for every event type.
  * `ASOE_CASE_AGENT_ENABLED=1` + EMAIL_ORDER_ENTRY_REQUEST →
    case-agent path (case opened, agent harness invoked).
  * `ASOE_CASE_AGENT_ENABLED=1` + non-routable event → still
    deterministic graph (predicate restricts the routable set).
  * Compliance Shadow runs on the agent path (CLAUDE.md §4 — never
    bypassed).
  * Truthy-variant matrix on the env var.

The agent path uses `StubAgentLLMProvider` until the Azure-backed
provider lands in Step 7. Tests don't need a real LLM; the
StubAgentLLMProvider with an empty script yields ERROR (no
tool calls), which the dispatch maps to FAIL_TO_HUMAN — that's
the expected outcome for a routing wire-up smoke test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import case_store, exception_store
from compliance.shadow_llm import shadow_llm_cache, shadow_llm_metrics


@pytest.fixture(autouse=True)
def _reset_state():
    case_store.clear()
    exception_store.clear()
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()
    yield
    case_store.clear()
    exception_store.clear()
    shadow_llm_cache.clear()
    shadow_llm_metrics.reset()


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def manager_token():
    return create_test_token(roles=["manager"], org="tenant-a")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _email_order_event() -> dict:
    return {
        "order_id": "PO-EOE-1",
        "po_price": 100.0,
        "sap_base_price": 100.0,
        "event_type": "EMAIL_ORDER_ENTRY_REQUEST",
        "retailer_id": "acc-001",
    }


def _pricing_event() -> dict:
    return {
        "order_id": "PO-PRICE-1",
        "po_price": 90.0,
        "sap_base_price": 100.0,
        "event_type": "EDI_850_PRICE_MISMATCH",
    }


# ---------------------------------------------------------------------------
# Default (predicate disabled) — deterministic graph for every event type
# ---------------------------------------------------------------------------

class TestDefaultDisabled:
    def test_email_order_entry_uses_graph_when_flag_off(
        self, client, manager_token, monkeypatch,
    ):
        """Both paths open a case (the deterministic graph
        materialises lazily on non-clean events too); the
        discriminator is tier graduation. The harness always
        graduates T2 → T3 on a non-clean event; the deterministic
        graph leaves the case at T2."""
        monkeypatch.delenv("ASOE_CASE_AGENT_ENABLED", raising=False)
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_email_order_event(),
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        cases = case_store.list_by_tenant("tenant-a")
        for c in cases:
            assert c.tier == 2, "harness should not have run"

    def test_pricing_event_uses_graph_when_flag_off(
        self, client, manager_token, monkeypatch,
    ):
        monkeypatch.delenv("ASOE_CASE_AGENT_ENABLED", raising=False)
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_pricing_event(),
            headers=_auth(manager_token),
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Predicate enabled — only EMAIL_ORDER_ENTRY_REQUEST routes
# ---------------------------------------------------------------------------

class TestPredicateEnabled:
    def test_email_order_entry_routed_to_case_agent(
        self, client, manager_token, monkeypatch,
    ):
        monkeypatch.setenv("ASOE_CASE_AGENT_ENABLED", "1")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_email_order_event(),
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        cases = case_store.list_by_tenant("tenant-a")
        assert len(cases) == 1
        case = cases[0]
        assert case.source == "manual_order"
        assert case.source_channel == "email"
        # Harness ran → tier graduated T2 → T3 on the non-clean event.
        assert case.tier == 3

    def test_pricing_event_still_uses_graph_when_flag_on(
        self, client, manager_token, monkeypatch,
    ):
        """Predicate restricts the routable set; pricing event
        still goes through the deterministic graph (case left at T2)."""
        monkeypatch.setenv("ASOE_CASE_AGENT_ENABLED", "1")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_pricing_event(),
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        for c in case_store.list_by_tenant("tenant-a"):
            assert c.tier == 2


# ---------------------------------------------------------------------------
# Truthy-variant matrix on the env var
# ---------------------------------------------------------------------------

class TestEnvVarVariants:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_truthy_values_enable(
        self, client, manager_token, monkeypatch, value,
    ):
        monkeypatch.setenv("ASOE_CASE_AGENT_ENABLED", value)
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_email_order_event(),
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        cases = case_store.list_by_tenant("tenant-a")
        assert len(cases) == 1
        # Harness ran → tier graduated T2 → T3.
        assert cases[0].tier == 3, value

    @pytest.mark.parametrize("value", ["0", "false", "no", "", " "])
    def test_falsy_values_keep_graph(
        self, client, manager_token, monkeypatch, value,
    ):
        monkeypatch.setenv("ASOE_CASE_AGENT_ENABLED", value)
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_email_order_event(),
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        # Harness did not run → tier stays at T2 (the
        # deterministic graph still materialises the case lazily
        # but doesn't graduate it).
        for c in case_store.list_by_tenant("tenant-a"):
            assert c.tier == 2, value


# ---------------------------------------------------------------------------
# Compliance Shadow is mandatory on the agent path (CLAUDE.md §4)
# ---------------------------------------------------------------------------

class TestComplianceShadowOnAgentPath:
    def test_agent_path_runs_l1_shadow(
        self, client, manager_token, monkeypatch,
    ):
        """The agent path must NOT bypass L1 deterministic Shadow.
        Verified by checking the persisted exception record carries
        a shadow verdict (any of GREEN/YELLOW/RED) on the agent
        path."""
        monkeypatch.setenv("ASOE_CASE_AGENT_ENABLED", "1")
        r = client.post(
            "/api/v1/exceptions/resolve",
            json=_email_order_event(),
            headers=_auth(manager_token),
        )
        assert r.status_code == 200
        body = r.json()
        # The L1 Shadow verdict surfaces on the response — the
        # agent path goes through the same persistence /
        # response-shaping as the graph path.
        assert body["shadow_verdict"] in ("GREEN", "YELLOW", "RED")
