# Phase 7a — Infrastructure Gateways

```text
Read architecture_v3.md, CLAUDE.md, and tasks.md (Phase 7.1 and 7.3).
Implement only Phase 7.1 and 7.3.

Requirements:
- define InfrastructureGateway as a @runtime_checkable Protocol (Port) in gateways/base.py
- implement gateway registry, GatewayExecutor (structured tracing + error handling), and StubGateway test double
- add typed contracts: GatewayRequest, GatewayResponse, GatewayDependency, GatewayEffect
- extend RecipeSpec with optional dependencies and effects tuples
- add resolve_dependencies node (pre-recipe; gateway failure → FAIL_TO_HUMAN)
- add apply_effects node (post-recipe; effect failure is logged, does not roll back)
- wire: validate_types → resolve_dependencies → execute_recipe → apply_effects → END
- add gateway_calls to TraceRecord
- add DUPLICATE_PO branch to DeterministicFallbackBackend (classify_intent + propose_recipe)

Constraints:
- recipes must never import from gateways/
- StubGateway only in tests — no real network calls in CI
- all new contracts use extra="forbid"
- do not add speculative features beyond tasks.md

Add tests for: registry, executor success/failure, StubGateway call recording,
resolve_dependencies (success + failure → FAIL_TO_HUMAN), apply_effects (effect
failure does not alter recipe result), DUPLICATE_PO end-to-end routing.
```
