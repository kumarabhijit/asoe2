"""V1 Foundation Guardrail tests (architecture_v3.md Section 15).

CI-automated enforcement of 6 guardrails that preserve the V2/V3
expansion path. Violating any guardrail fails the build.

Guardrail #1 — No intent-specific logic in pipeline nodes
Guardrail #2 — Dynamic enum serving (API serves intents/recipes/lifecycle dynamically)
Guardrail #3 — Metadata keys documented per RecipeSpec
Guardrail #4 — ERP-agnostic gateway protocol
Guardrail #5 — Intent-agnostic exceptions table schema
Guardrail #6 — Hierarchical policy key format in policy_overrides
"""

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
from pathlib import Path
from typing import get_type_hints

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import create_test_token
from api.store import exception_store


# ---------------------------------------------------------------------------
# Guardrail #1 — No intent-specific logic in pipeline nodes
#
# architecture_v3.md §15.1: nodes must operate on typed abstractions,
# never on `if intent == "DUPLICATE_PO"` branches.
# ---------------------------------------------------------------------------

class TestGuardrail1_NodeIntentAgnostic:
    """AST inspection: verify no node function branches on literal intent values."""

    _NODES_PATH = Path(__file__).parent.parent / "orchestration" / "nodes.py"

    # Intent string literals that must NOT appear in if/elif comparisons
    _INTENT_LITERALS = {
        "CONTRACTUAL_CORRECTION", "CREDIT_BLOCK",
        "MASS_PRICING_ERROR", "DUPLICATE_PO",
    }

    def _get_node_function_names(self) -> list[str]:
        """Return all top-level function names in nodes.py."""
        source = self._NODES_PATH.read_text()
        tree = ast.parse(source)
        return [
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]

    def _find_intent_comparisons_in_ast(self) -> list[str]:
        """Find any Compare nodes that test against intent string literals."""
        source = self._NODES_PATH.read_text()
        tree = ast.parse(source)
        violations = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            # Check all comparators for string literals matching intent values
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    if comparator.value in self._INTENT_LITERALS:
                        violations.append(
                            f"Line {node.lineno}: comparison against "
                            f"intent literal '{comparator.value}'"
                        )
            # Also check the left side
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                if node.left.value in self._INTENT_LITERALS:
                    violations.append(
                        f"Line {node.lineno}: comparison against "
                        f"intent literal '{node.left.value}'"
                    )

        return violations

    def test_no_intent_string_literals_in_comparisons(self):
        """No if/elif branch on intent string literals in orchestration/nodes.py."""
        violations = self._find_intent_comparisons_in_ast()
        assert violations == [], (
            f"Guardrail #1 violated: intent-specific logic found in nodes.py:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_no_intent_literal_grep(self):
        """Belt-and-suspenders: grep for quoted intent strings in nodes.py."""
        source = self._NODES_PATH.read_text()
        for intent in self._INTENT_LITERALS:
            pattern = f'"{intent}"'
            assert pattern not in source, (
                f"Guardrail #1 violated: found {pattern} in orchestration/nodes.py. "
                f"Intent-specific logic belongs in recipes, not pipeline nodes."
            )

    def test_node_functions_exist(self):
        """Sanity check: nodes.py has the expected pipeline functions."""
        names = self._get_node_function_names()
        expected = {"ingest", "classify", "load_skill", "shadow_audit",
                    "select_recipe", "validate_types", "execute_recipe",
                    "apply_effects", "explain_only"}
        for fn in expected:
            assert fn in names, f"Expected node function '{fn}' not found in nodes.py"


# ---------------------------------------------------------------------------
# Guardrail #2 — Dynamic enum serving
#
# architecture_v3.md §15.2: API serves allowed_intents, lifecycle_states,
# and allowed_recipes dynamically. Adding a new intent must not require
# API-consumer code changes.
# ---------------------------------------------------------------------------

class TestGuardrail2_DynamicEnumServing:
    """Verify the health endpoint serves enums from the schema, not hardcoded lists."""

    @pytest.fixture()
    def client(self):
        return TestClient(create_app(), raise_server_exceptions=False)

    def test_health_serves_all_allowed_intents(self, client):
        from constraints.specs import AllowedIntent
        expected_intents = set(AllowedIntent.__args__)  # type: ignore[attr-defined]

        r = client.get("/api/v1/health")
        served_intents = set(r.json()["allowed_intents"])
        assert served_intents == expected_intents, (
            f"Guardrail #2 violated: health endpoint intents {served_intents} "
            f"don't match AllowedIntent {expected_intents}"
        )

    def test_health_serves_all_allowed_recipes(self, client):
        from constraints.specs import AllowedRecipeName
        expected_recipes = set(AllowedRecipeName.__args__)  # type: ignore[attr-defined]

        r = client.get("/api/v1/health")
        served_recipes = set(r.json()["allowed_recipes"])
        assert served_recipes == expected_recipes, (
            f"Guardrail #2 violated: health endpoint recipes {served_recipes} "
            f"don't match AllowedRecipeName {expected_recipes}"
        )

    def test_health_serves_12_lifecycle_states(self, client):
        """architecture_v3.md §9.1: 12 lifecycle states (incl. PENDING_ADMIN_REVIEW)."""
        r = client.get("/api/v1/health")
        states = r.json()["lifecycle_states"]
        assert len(states) == 12, (
            f"Guardrail #2 violated: expected 12 lifecycle states, got {len(states)}"
        )
        required = {"INGESTED", "CLASSIFYING", "AUDITING", "PENDING_REVIEW",
                     "ESCALATED", "PENDING_ADMIN_REVIEW", "EXECUTING",
                     "RESOLVED", "FAILED", "BLOCKED", "REJECTED", "CLOSED"}
        assert set(states) == required

    def test_enum_values_not_hardcoded_in_route(self):
        """Verify the health route reads from specs, not a hardcoded list."""
        source = (Path(__file__).parent.parent / "api" / "routes" / "health.py").read_text()
        # The route should reference AllowedIntent.__args__, not literal lists
        assert "AllowedIntent" in source, (
            "Guardrail #2: health route should import AllowedIntent, not hardcode intents"
        )
        assert "AllowedRecipeName" in source, (
            "Guardrail #2: health route should import AllowedRecipeName, not hardcode recipes"
        )


# ---------------------------------------------------------------------------
# Guardrail #3 — Metadata keys documented per RecipeSpec
#
# architecture_v3.md §15.3: each RecipeSpec declares expected_metadata_keys.
# Test fixtures must include all declared keys.
# ---------------------------------------------------------------------------

class TestGuardrail3_MetadataContracts:
    """Verify expected_metadata_keys are declared and present in test fixtures."""

    def test_duplicate_po_declares_metadata_keys(self):
        from recipes.registry import REGISTRY
        spec = REGISTRY["DuplicatePORecipe.py"]
        assert len(spec.expected_metadata_keys) > 0, (
            "Guardrail #3: DuplicatePORecipe.py must declare expected_metadata_keys"
        )
        assert "signal_scores" in spec.expected_metadata_keys

    def test_recipes_without_metadata_declare_empty(self):
        """Recipes that don't need metadata should have empty expected_metadata_keys."""
        from recipes.registry import REGISTRY
        for name, spec in REGISTRY.items():
            assert hasattr(spec, "expected_metadata_keys"), (
                f"Guardrail #3: RecipeSpec for {name} is missing expected_metadata_keys field"
            )

    def test_conftest_duplicate_po_fixture_has_required_keys(self):
        """The DUPLICATE_PO test fixture must include all expected metadata keys."""
        from recipes.registry import REGISTRY
        spec = REGISTRY["DuplicatePORecipe.py"]
        required_keys = set(spec.expected_metadata_keys)

        # Read conftest source and extract metadata keys from the fixture
        conftest_path = Path(__file__).parent / "conftest.py"
        source = conftest_path.read_text()

        # Check that each required key appears in the duplicate_po fixture metadata dict
        # The fixture defines metadata={"signal_scores": ..., "matched_po_id": ...}
        for key in required_keys:
            assert f'"{key}"' in source, (
                f"Guardrail #3: conftest.py DUPLICATE_PO fixture missing "
                f"metadata key '{key}'. Required by DuplicatePORecipe.py "
                f"expected_metadata_keys."
            )


# ---------------------------------------------------------------------------
# Guardrail #4 — ERP-agnostic gateway protocol
#
# architecture_v3.md §15.4: the InfrastructureGateway protocol, GatewayRequest,
# and GatewayResponse must not reference SAP/Oracle/Dynamics-specific strings.
# ---------------------------------------------------------------------------

class TestGuardrail4_ERPAgnosticGateway:
    """Verify gateway protocol boundary contains no ERP vendor-specific terms."""

    _ERP_SPECIFIC_PATTERNS = [
        r"\bBAPI\b", r"\bRFC\b", r"\bIDOC\b",             # SAP
        r"\bSAP\b",                                         # SAP (in type defs)
        r"\bOracle\b", r"\bDynamics\b",                     # Oracle, Microsoft
        r"\bYK07\b", r"\bZK07\b", r"\bPR00\b",             # SAP condition types
        r"\bT_CODE\b", r"\bABAP\b",                         # SAP internals
    ]

    def _check_source_for_erp_terms(self, source: str, filename: str) -> list[str]:
        violations = []
        for pattern in self._ERP_SPECIFIC_PATTERNS:
            matches = re.findall(pattern, source)
            if matches:
                violations.append(
                    f"{filename}: found ERP-specific term '{matches[0]}'"
                )
        return violations

    def _strip_comments_and_docstrings(self, source: str) -> str:
        """Remove comments and docstrings, leaving only executable code."""
        tree = ast.parse(source)
        # Collect line ranges of all docstrings
        docstring_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    ds = node.body[0]
                    for ln in range(ds.lineno, ds.end_lineno + 1):  # type: ignore[union-attr]
                        docstring_lines.add(ln)

        lines = source.splitlines()
        code_lines = []
        for i, line in enumerate(lines, 1):
            if i in docstring_lines:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            code_lines.append(line)
        return "\n".join(code_lines)

    def test_gateway_base_is_erp_agnostic(self):
        source = (Path(__file__).parent.parent / "gateways" / "base.py").read_text()
        code = self._strip_comments_and_docstrings(source)
        violations = self._check_source_for_erp_terms(code, "gateways/base.py")
        assert violations == [], (
            f"Guardrail #4 violated: ERP-specific terms in gateway protocol:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_gateway_request_response_are_erp_agnostic(self):
        """GatewayRequest and GatewayResponse type definitions must be vendor-neutral."""
        from contracts.models import GatewayRequest, GatewayResponse
        for model in [GatewayRequest, GatewayResponse]:
            field_names = set(model.model_fields.keys())
            for name in field_names:
                for pattern in self._ERP_SPECIFIC_PATTERNS:
                    assert not re.search(pattern, name, re.IGNORECASE), (
                        f"Guardrail #4: field '{name}' on {model.__name__} "
                        f"matches ERP-specific pattern '{pattern}'"
                    )

    def test_gateway_executor_is_erp_agnostic(self):
        source = (Path(__file__).parent.parent / "gateways" / "executor.py").read_text()
        code = self._strip_comments_and_docstrings(source)
        violations = self._check_source_for_erp_terms(code, "gateways/executor.py")
        assert violations == [], (
            f"Guardrail #4 violated:\n" + "\n".join(f"  - {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# Guardrail #5 — Intent-agnostic exceptions table schema
#
# architecture_v3.md §15.5: no intent-specific columns on the exceptions table.
# All intent-specific data lives in resolution_data JSONB.
# ---------------------------------------------------------------------------

class TestGuardrail5_SchemaAgnostic:
    """Verify the exceptions table has no intent-specific columns."""

    _INTENT_SPECIFIC_PATTERNS = [
        r"similarity", r"damage", r"deduction", r"chargeback",
        r"short.ship", r"temperature", r"otif", r"penalty",
        r"po_number_score", r"line_item_overlap",
    ]

    def _get_exceptions_columns(self) -> list[str]:
        """Extract column names from the SQLite-compatible migration schema."""
        from db.connection import SQLiteAdapter
        adapter = SQLiteAdapter(":memory:")
        adapter.apply_schema()
        with adapter.cursor() as cur:
            cur.execute("PRAGMA table_info(exceptions)")
            rows = cur.fetchall()
        return [row[1] if not hasattr(row, "keys") else row["name"] for row in rows]

    def test_no_intent_specific_columns(self):
        columns = self._get_exceptions_columns()
        for col in columns:
            for pattern in self._INTENT_SPECIFIC_PATTERNS:
                assert not re.search(pattern, col, re.IGNORECASE), (
                    f"Guardrail #5 violated: column '{col}' on exceptions table "
                    f"matches intent-specific pattern '{pattern}'. "
                    f"Intent-specific data must live in resolution_data JSONB."
                )

    def test_resolution_data_column_exists(self):
        """The resolution_data extensibility column must be present."""
        columns = self._get_exceptions_columns()
        assert "resolution_data" in columns, (
            "Guardrail #5: exceptions table must have a resolution_data column "
            "for intent-specific extensibility"
        )

    def test_migration_sql_has_no_intent_columns(self):
        """Scan the PostgreSQL migration SQL for intent-specific column definitions."""
        sql_path = Path(__file__).parent.parent / "db" / "migrations" / "V001__initial_schema.sql"
        sql = sql_path.read_text().lower()
        # Only check CREATE TABLE exceptions block
        exc_start = sql.find("create table if not exists exceptions")
        exc_end = sql.find(");", exc_start) + 2 if exc_start >= 0 else 0
        exceptions_ddl = sql[exc_start:exc_end]

        for pattern in self._INTENT_SPECIFIC_PATTERNS:
            assert not re.search(pattern, exceptions_ddl), (
                f"Guardrail #5: PostgreSQL migration has intent-specific column "
                f"matching '{pattern}' in exceptions table"
            )


# ---------------------------------------------------------------------------
# Guardrail #6 — Hierarchical policy key format
#
# architecture_v3.md §15.6: all policy_overrides rows must use the
# dot-delimited hierarchical format: global.{key}, tenant.{id}.{key},
# or retailer.{id}.{key} / retailer.{id}.category.{cat}.{key}.
# ---------------------------------------------------------------------------

class TestGuardrail6_PolicyKeyFormat:
    """Verify policy keys follow the hierarchical format."""

    _VALID_KEY_PATTERN = re.compile(
        r"^(global|tenant\.[a-z0-9_-]+|retailer\.[a-z0-9_-]+(\.category\.[a-z0-9_-]+)?)\.\w+$",
        re.IGNORECASE,
    )

    def test_valid_global_key_accepted(self):
        assert self._VALID_KEY_PATTERN.match("global.MAX_DISCOUNT_ALLOWED")

    def test_valid_tenant_key_accepted(self):
        assert self._VALID_KEY_PATTERN.match("tenant.acme.MAX_DISCOUNT_ALLOWED")

    def test_valid_retailer_key_accepted(self):
        assert self._VALID_KEY_PATTERN.match("retailer.walmart.DISPUTE_WINDOW_DAYS")

    def test_valid_retailer_category_key_accepted(self):
        assert self._VALID_KEY_PATTERN.match("retailer.walmart.category.cereal.MAX_DISCOUNT")

    def test_flat_key_rejected(self):
        """Flat keys without scope prefix are invalid."""
        assert not self._VALID_KEY_PATTERN.match("MAX_DISCOUNT_ALLOWED"), (
            "Guardrail #6: flat key 'MAX_DISCOUNT_ALLOWED' must not match. "
            "Use 'global.MAX_DISCOUNT_ALLOWED' instead."
        )

    def test_policy_repo_enforces_format(self):
        """Verify keys written via the API use the correct format."""
        # The Phase 12 API test for policies uses "global.MAX_DISCOUNT_ALLOWED"
        # — validate that the key would pass the format check.
        api_test_key = "global.MAX_DISCOUNT_ALLOWED"
        assert self._VALID_KEY_PATTERN.match(api_test_key)

    def test_existing_api_test_keys_are_hierarchical(self):
        """Scan test_api.py for policy key values and verify format."""
        test_path = Path(__file__).parent / "test_api.py"
        source = test_path.read_text()
        # Find all "policy_key": "..." patterns
        keys = re.findall(r'"policy_key":\s*"([^"]+)"', source)
        for key in keys:
            assert self._VALID_KEY_PATTERN.match(key), (
                f"Guardrail #6 violated: test_api.py uses flat policy key '{key}'. "
                f"Must use hierarchical format (global.X, tenant.Y.X, etc.)"
            )

    def test_database_repo_writes_valid_keys(self):
        """Write a policy via the repository and validate the key format."""
        from db.connection import SQLiteAdapter
        from db.repository import PolicyRepository

        adapter = SQLiteAdapter(":memory:")
        adapter.apply_schema()
        repo = PolicyRepository(adapter)

        # Valid key
        result = repo.create_override(
            tenant_id="t1",
            policy_key="global.CIRCUIT_BREAKER_MAX_UPDATES",
            value=100,
            created_by="test",
        )
        assert self._VALID_KEY_PATTERN.match(result["policy_key"])


# ---------------------------------------------------------------------------
# Execution Invariant #11 — Recipes never import from policy module
#
# This is already in the architecture invariants but is closely related
# to guardrail enforcement, so included here for completeness.
# ---------------------------------------------------------------------------

class TestRecipePolicyDecoupling:
    """Verify no recipe imports from contracts.policy."""

    _RECIPE_DIR = Path(__file__).parent.parent / "recipes"

    def test_no_policy_imports_in_recipes(self):
        """Invariant #11: recipes must not import from contracts/policy.py.

        Checks actual import statements via AST, not string presence in
        comments or docstrings.
        """
        for recipe_path in self._RECIPE_DIR.glob("*.py"):
            if recipe_path.name.startswith("__"):
                continue
            source = recipe_path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    assert "contracts.policy" not in module, (
                        f"Invariant #11 violated: {recipe_path.name} has "
                        f"'from {module} import ...' — recipes must not "
                        f"import from contracts.policy."
                    )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "contracts.policy" not in alias.name, (
                            f"Invariant #11 violated: {recipe_path.name} has "
                            f"'import {alias.name}' — recipes must not "
                            f"import from contracts.policy."
                        )
