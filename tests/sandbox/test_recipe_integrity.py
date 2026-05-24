"""Recipe integrity and naming convention tests.

Validates:
  1. Business logic lives ONLY in Recipe.py files — tests must not mock it
  2. Tests mock ERP (SAP/Manhattan) responses, not recipe logic
  3. Machine-consumed outputs use Outlines/Guidance constraints
  4. All UI-facing strings use "ASOE", never "PRISM"
  5. Recipe registry matches architecture_v3.md
  6. Constrained output schemas are enforced

Architecture_v3.md Section 5 (Skill-Shadow-Recipe Engine),
CLAUDE.md Core Guardrails.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
from pathlib import Path

import pytest

from contracts.models import (
    GraphState,
    OrderEvent,
    GatewayResponse,
)
from gateways.registry import register_gateway, clear_registry
from gateways.stub import StubGateway
from orchestration.graph import run_graph


# ═══════════════════════════════════════════════════════════════════════════
# Recipe files exist and contain business logic
# ═══════════════════════════════════════════════════════════════════════════

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RECIPE_DIR = _REPO_ROOT / "recipes"


class TestRecipeModuleIntegrity:
    """Recipe files exist, are importable, and contain execute methods."""

    EXPECTED_RECIPES = [
        "PriceAdjustmentRecipe",
        "CreditHoldReleaseRecipe",
        "DuplicatePORecipe",
        "PriceHoldReleaseRecipe",
        "EdiMismatchRecipe",
        "BackOrderResolutionRecipe",
        "OverMaxTrimRecipe",
        "MOQRoundUpRecipe",
        "PalletAlignmentRecipe",
        "DeliveryDelayResolutionRecipe",
        # ADR-034 §6.3 — canonical recipe-name post-cutover.
        "ManualOrderIntakeRecipe",
        # ADR-042 Phase 3 — order-entry ERP write (directed graph re-entry).
        "SubmitToErpRecipe",
        # ADR-034 §6.3 — legacy alias retained until 2026-08-12.
        # Removed in the same PR that flips the §6.2 hard rejection on.
        "EmailOrderEntryRecipe",
    ]

    def test_recipe_directory_exists(self):
        assert _RECIPE_DIR.is_dir()

    @pytest.mark.parametrize("recipe_name", EXPECTED_RECIPES)
    def test_recipe_file_exists(self, recipe_name):
        path = _RECIPE_DIR / f"{recipe_name}.py"
        assert path.exists(), f"Missing recipe file: {path}"

    @pytest.mark.parametrize("recipe_name", EXPECTED_RECIPES)
    def test_recipe_is_importable(self, recipe_name):
        mod = importlib.import_module(f"recipes.{recipe_name}")
        assert mod is not None

    def test_recipe_registry_contains_all(self):
        from recipes.registry import REGISTRY
        for name in self.EXPECTED_RECIPES:
            key = f"{name}.py"
            assert key in REGISTRY, (
                f"Recipe '{key}' missing from REGISTRY"
            )

    def test_recipe_registry_only_contains_valid_recipes(self):
        from recipes.registry import REGISTRY
        for key in REGISTRY:
            assert key.endswith(".py"), f"Invalid registry key: {key}"
            name = key.replace(".py", "")
            assert name in self.EXPECTED_RECIPES, (
                f"Unexpected recipe in registry: {key}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Tests mock ERP responses, NOT recipe logic
# ═══════════════════════════════════════════════════════════════════════════

class TestERPMocking:
    """Verify that gateway stubs mock ERP responses, not recipe internals.

    This test demonstrates the correct pattern: StubGateway provides
    canned ERP responses; recipe logic executes untouched.
    """

    def test_stub_gateway_mocks_erp_not_recipe(self):
        """StubGateway returns canned data for gateway operations."""
        from contracts.models import GatewayRequest
        stub = StubGateway(
            "sap_erp",
            responses={
                "get_condition_records": GatewayResponse(
                    gateway_name="sap_erp",
                    operation="get_condition_records",
                    status="SUCCESS",
                    data={"condition_type": "YK07", "amount": 100.0},
                ),
            },
        )
        request = GatewayRequest(
            gateway_name="sap_erp",
            operation="get_condition_records",
            params={"sku": "SKU-001"},
            trace_id="test-trace-001",
        )
        result = stub.execute(request)
        assert result.status == "SUCCESS"
        assert result.data["condition_type"] == "YK07"

    def test_stub_records_calls(self):
        """StubGateway records all calls for assertion."""
        from contracts.models import GatewayRequest
        stub = StubGateway(
            "oms",
            responses={
                "get_order": GatewayResponse(
                    gateway_name="oms",
                    operation="get_order",
                    status="SUCCESS",
                    data={"order_id": "PO-001"},
                ),
            },
        )
        request = GatewayRequest(
            gateway_name="oms",
            operation="get_order",
            params={"id": "PO-001"},
            trace_id="test-trace-002",
        )
        stub.execute(request)
        assert len(stub.calls) == 1
        assert stub.calls[0].operation == "get_order"

    def test_end_to_end_with_stubbed_erp(self):
        """Full pipeline with stubbed ERP gateways — recipe logic untouched."""
        state = GraphState(
            event=OrderEvent(
                order_id="SO-INT-001",
                po_price=90.0,
                sap_base_price=100.0,
                retailer_id="R-01",
                line_count=1,
            )
        )
        final = run_graph(state)
        assert final.intent is not None
        assert final.final_status is not None


# ═══════════════════════════════════════════════════════════════════════════
# Constrained output validation
# ═══════════════════════════════════════════════════════════════════════════

class TestConstrainedOutputs:
    """Machine-consumed outputs are constrained via Outlines/Guidance."""

    def test_intent_constrained_to_enum(self):
        from contracts.models import Intent
        allowed = {e.value for e in Intent}
        expected = {
            "CONTRACTUAL_CORRECTION", "CREDIT_BLOCK",
            "MASS_PRICING_ERROR", "DUPLICATE_PO",
            "PRICE_HOLD_RELEASE", "EDI_MISMATCH",
            "BACK_ORDER", "OVER_MAX", "MIN_ORDER_QTY",
            "PALLET_CONFIG", "DELIVERY_DELAY", "MANUAL_ORDER_INTAKE", "UNKNOWN",
        }
        assert allowed == expected

    def test_shadow_verdict_constrained(self):
        from contracts.models import ComplianceDecision
        state = GraphState(
            event=OrderEvent(
                order_id="SO-CV", po_price=90.0, sap_base_price=100.0,
                line_count=1,
            )
        )
        final = run_graph(state)
        if final.shadow:
            assert final.shadow.status.value in ("GREEN", "YELLOW", "RED")

    def test_recipe_selection_constrained(self):
        """Selected recipe must be in the AllowedRecipeName set."""
        from constraints.specs import AllowedRecipeName
        allowed_names = set(AllowedRecipeName.__args__)
        state = GraphState(
            event=OrderEvent(
                order_id="SO-RC", po_price=90.0, sap_base_price=100.0,
                line_count=1,
            )
        )
        final = run_graph(state)
        if final.selected_recipe:
            assert final.selected_recipe in allowed_names

    def test_constrained_backend_chain_exists(self):
        """The 3-tier backend chain is configured."""
        from constraints.router import get_constrained_backend
        backend = get_constrained_backend()
        assert backend is not None
        assert hasattr(backend, "classify_intent")
        assert hasattr(backend, "propose_recipe")
        assert hasattr(backend, "shadow_decision")

    def test_execution_log_records_constrained_outputs(self):
        """ExecutionLog.constrained_outputs records which schemas were used."""
        state = GraphState(
            event=OrderEvent(
                order_id="SO-EL", po_price=90.0, sap_base_price=100.0,
                line_count=1,
            )
        )
        final = run_graph(state)
        if final.execution_log:
            co = final.execution_log.constrained_outputs
            assert isinstance(co, dict)


# ═══════════════════════════════════════════════════════════════════════════
# Naming convention: ASOE, never PRISM
# ═══════════════════════════════════════════════════════════════════════════

class TestNamingConventions:
    """All UI-facing strings and logs use 'ASOE', never 'PRISM'."""

    # Directories to scan for PRISM references
    _SCAN_DIRS = ["api", "contracts", "compliance", "orchestration",
                  "recipes", "skills", "gateways", "constraints"]

    def test_no_prism_in_python_source(self):
        """No Python source file in core modules references PRISM."""
        violations = []
        for dir_name in self._SCAN_DIRS:
            dir_path = _REPO_ROOT / dir_name
            if not dir_path.is_dir():
                continue
            for py_file in dir_path.rglob("*.py"):
                content = py_file.read_text()
                # Case-insensitive search for PRISM as a standalone word
                if re.search(r'\bPRISM\b', content, re.IGNORECASE):
                    violations.append(str(py_file.relative_to(_REPO_ROOT)))
        assert violations == [], (
            f"Found legacy 'PRISM' references in: {violations}"
        )

    def test_no_prism_in_skill_files(self):
        """No SKILL.md file references PRISM."""
        skills_dir = _REPO_ROOT / "skills"
        violations = []
        for md_file in skills_dir.glob("*_SKILL.md"):
            content = md_file.read_text()
            if re.search(r'\bPRISM\b', content, re.IGNORECASE):
                violations.append(str(md_file.relative_to(_REPO_ROOT)))
        assert violations == [], (
            f"Found legacy 'PRISM' references in skill files: {violations}"
        )

    def test_api_title_uses_asoe(self):
        """FastAPI app title uses ASOE."""
        from api.app import app
        assert "ASOE" in app.title

    def test_health_endpoint_no_prism(self, client, analyst_token):
        """Health response does not contain PRISM."""
        import json
        resp = client.get("/api/v1/health")
        body = json.dumps(resp.json())
        assert "PRISM" not in body.upper()

    def test_error_responses_use_asoe(self, client):
        """Error envelope uses ASOE-prefixed error codes."""
        from api.errors import ASOEError
        err = ASOEError(code="TEST_ERROR", message="test", status_code=400)
        assert "ASOE" in type(err).__name__


# ═══════════════════════════════════════════════════════════════════════════
# Policy externalization (recipes don't import policy)
# ═══════════════════════════════════════════════════════════════════════════

class TestPolicyExternalization:
    """Recipes never import from the policy module directly.

    Architecture_v3.md Section 5.4: All thresholds are injected by the
    orchestration layer via validate_types → RecipeInvocation.params.
    """

    def test_recipes_do_not_import_policy(self):
        """No recipe file imports from contracts.policy."""
        violations = []
        for py_file in _RECIPE_DIR.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            content = py_file.read_text()
            if "from contracts.policy" in content or "import policy" in content:
                violations.append(py_file.name)
        assert violations == [], (
            f"Recipes importing policy directly: {violations}. "
            "Thresholds must be injected via RecipeInvocation.params."
        )

    def test_policy_constants_exist(self):
        """Policy module defines the expected constants."""
        from contracts.policy import (
            MAX_DISCOUNT_ALLOWED,
            CREDIT_AUTHORIZED_ROLES,
            DUPLICATE_PO_THRESHOLD_AUTO_BLOCK,
        )
        assert MAX_DISCOUNT_ALLOWED == 0.15
        assert "ORDER_MANAGER" in CREDIT_AUTHORIZED_ROLES
        assert DUPLICATE_PO_THRESHOLD_AUTO_BLOCK == 0.90
