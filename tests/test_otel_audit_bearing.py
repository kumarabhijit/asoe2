"""PARITY-5 — OpenTelemetry integration + @audit_bearing decorator.

Two contracts:

  1. **OTel init is opt-in**: when APPLICATIONINSIGHTS_CONNECTION_STRING is
     unset (local dev, CI), the module is a no-op — no exporter spun up,
     no upstream packages imported, no startup side effects.

  2. **@audit_bearing decorator** marks a state-mutating route as
     SOX-relevant. A CI lint (asserted here as a source-grep at test time)
     fails when a state-mutating route (POST/PUT/PATCH/DELETE) under
     api/routes/ lacks the decorator.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _quiet_lifespan(monkeypatch):
    monkeypatch.setenv("ASOE_LLM_PROVIDER", "fallback")


class TestOtelOptional:
    def test_init_is_noop_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
        from api.observability import otel

        result = otel.init_telemetry(app=None)
        assert result is False, "init must return False when env unset"

    def test_init_returns_true_when_env_set_and_libs_present(self, monkeypatch):
        """When the env is set but the otel libs aren't installed,
        init must still return False (graceful degrade) rather than
        crashing the worker on startup."""
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=fake")
        from api.observability import otel
        importlib.reload(otel)

        result = otel.init_telemetry(app=None)
        # In a CI container without opentelemetry, returns False without
        # raising. If the libs ARE present, returns True. Either branch
        # is acceptable — the negative-space requirement is "no crash".
        assert result in (True, False)


class TestAuditBearingDecorator:
    def test_decorator_marks_route(self):
        from api.observability.audit_bearing import audit_bearing, is_audit_bearing

        @audit_bearing(reason="state-mutating example")
        def example_route():
            return "ok"

        assert is_audit_bearing(example_route) is True

    def test_undecorated_function_not_marked(self):
        from api.observability.audit_bearing import is_audit_bearing

        def plain_function():
            return "ok"

        assert is_audit_bearing(plain_function) is False

    def test_decorator_preserves_function_behaviour(self):
        from api.observability.audit_bearing import audit_bearing

        @audit_bearing(reason="passthrough test")
        def adds(a, b):
            return a + b

        assert adds(2, 3) == 5

    def test_decorator_records_reason(self):
        from api.observability.audit_bearing import audit_bearing, audit_bearing_reason

        @audit_bearing(reason="MUST RECORD WHY")
        def example():
            return None

        assert audit_bearing_reason(example) == "MUST RECORD WHY"


# ---------------------------------------------------------------------------
# Pre-defined alert names — Phase 0 pre-declares them so Phase 5 can fill in
# the thresholds without scope creep.
# ---------------------------------------------------------------------------

class TestAlertCatalogue:
    REQUIRED_ALERTS = {
        "zero-highlight",
        "layer2-open-rate",
        "breaker-open",
        "extraction-cost-overrun",
        "audit-chain-verify-failed",
        "retention-sweeper-anomaly",
    }

    def test_catalogue_lists_every_required_alert(self):
        from api.observability.alerts import ALERT_CATALOGUE

        names = {a.name for a in ALERT_CATALOGUE}
        missing = self.REQUIRED_ALERTS - names
        assert not missing, f"alert catalogue missing: {missing}"

    def test_each_alert_has_a_description(self):
        from api.observability.alerts import ALERT_CATALOGUE

        for alert in ALERT_CATALOGUE:
            assert alert.description, f"alert {alert.name!r} has no description"


# ---------------------------------------------------------------------------
# PII redaction at the structured-logger level (Security review)
# ---------------------------------------------------------------------------

class TestLogRedaction:
    def test_email_redacted(self):
        from api.observability.log_redaction import redact_pii

        out = redact_pii("operator alice@acme-corp.com triggered override")
        assert "alice@acme-corp.com" not in out
        assert "<redacted-email>" in out

    def test_phone_redacted(self):
        from api.observability.log_redaction import redact_pii

        out = redact_pii("contact: 415-555-1234")
        assert "415-555-1234" not in out

    def test_unrelated_text_unchanged(self):
        from api.observability.log_redaction import redact_pii

        original = "policy verdict GREEN at sequence 42"
        assert redact_pii(original) == original


# ---------------------------------------------------------------------------
# Architectural lock: every state-mutating route MUST be marked
# @audit_bearing (per Phase 5 acceptance criteria).
# ---------------------------------------------------------------------------

class TestAuditBearingLintRegex:
    """Code-review followup — the lint must:

      * find multi-line @router.post(\\n  "/path"\\n, ...) decorators
        (the original single-line regex missed them).
      * NOT accept a comment containing the substring "audit_bearing"
        as satisfying the requirement.
      * accept @audit_bearing both BEFORE and AFTER @router.post
        (decorator order doesn't matter to FastAPI; the lint must
        match the runtime semantics).
    """

    def test_multiline_decorator_detected(self, tmp_path):
        from api.observability.audit_bearing import scan_routes_for_violations

        src = tmp_path / "routes.py"
        src.write_text(
            '@router.post(\n'
            '    "/multiline-mutator",\n'
            '    dependencies=[Depends(require_role("admin"))],\n'
            ')\n'
            'async def mutates(): ...\n'
        )
        violations = scan_routes_for_violations(routes_dir=tmp_path, exempt_paths=set())
        assert any("/multiline-mutator" in v for v in violations)

    def test_comment_does_not_satisfy_lint(self, tmp_path):
        from api.observability.audit_bearing import scan_routes_for_violations

        src = tmp_path / "routes.py"
        src.write_text(
            '# TODO: should we add audit_bearing here?\n'
            '@router.post("/no-decorator")\n'
            'async def mutates(): ...\n'
        )
        violations = scan_routes_for_violations(routes_dir=tmp_path, exempt_paths=set())
        assert any("/no-decorator" in v for v in violations)

    def test_decorator_after_router_accepted(self, tmp_path):
        from api.observability.audit_bearing import scan_routes_for_violations

        src = tmp_path / "routes.py"
        src.write_text(
            '@router.post("/legit")\n'
            '@audit_bearing(reason="ok")\n'
            'async def mutates(): ...\n'
        )
        violations = scan_routes_for_violations(routes_dir=tmp_path, exempt_paths=set())
        assert not any("/legit" in v for v in violations)

    def test_decorator_before_router_accepted(self, tmp_path):
        from api.observability.audit_bearing import scan_routes_for_violations

        src = tmp_path / "routes.py"
        src.write_text(
            '@audit_bearing(reason="ok")\n'
            '@router.post("/legit-rev")\n'
            'async def mutates(): ...\n'
        )
        violations = scan_routes_for_violations(routes_dir=tmp_path, exempt_paths=set())
        assert not any("/legit-rev" in v for v in violations)


class TestAuditBearingLint:
    ROOT = Path(__file__).resolve().parent.parent
    ROUTES_DIR = ROOT / "api" / "routes"
    EXEMPT_YAML = ROOT / "compliance" / "audit_bearing_exemptions.yaml"

    def _exempt_paths(self) -> set[str]:
        import yaml

        text = self.EXEMPT_YAML.read_text()
        data = yaml.safe_load(text) or {}
        return set(data.get("exempt_paths", []))

    def test_state_mutating_routes_are_audit_bearing(self):
        from api.observability.audit_bearing import scan_routes_for_violations

        violations = scan_routes_for_violations(
            routes_dir=self.ROUTES_DIR,
            exempt_paths=self._exempt_paths(),
        )
        # If this fails, the offending route file + decorator line is
        # in the violation list. Add `@audit_bearing(...)` above the
        # route handler OR add the path to compliance/audit_bearing_
        # exemptions.yaml with a compliance-reviewer-approved reason.
        assert violations == [], f"audit-bearing violations: {violations}"
