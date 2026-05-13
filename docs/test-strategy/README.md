# asoe2 test strategy

## Purpose

Codify the test patterns the backend relies on, where each kind of
bug should be caught, and the rules new contributors must follow.
Companion to `asoe-ui/docs/test-strategy/README.md`.

The other files in this folder (`design.md`, `e2e-flow-plan.md`,
`eng-review-test-plan.md`) cover specific design and review
disciplines; this README is the overview and the gate list.

## The test pyramid

| Layer | Tool | Runtime | What it locks |
|---|---|---|---|
| **L0 — Pydantic / contract locks** | pytest unit | <1s | `model_validator` predicates, schema shape |
| **L1 — Unit tests** | pytest | seconds | Pure functions, recipes, gateways, audit chain |
| **L2 — Route handler tests** | pytest + `TestClient` | seconds | API handler + dependency-injection contract |
| **L3 — Orchestration tests** | pytest + `run_graph` | tens of seconds | LangGraph state transitions end-to-end (no LLM) |
| **L4 — Sandbox integration** | pytest in `tests/sandbox/` | tens of seconds | Full FastAPI app via `TestClient`; WebSocket, persistence, auth |
| **L5 — Cross-repo browser e2e** | Playwright in asoe-ui | minutes | Live API surface; UI ↔ backend contract |

L0–L4 run in `pytest tests/`. L5 runs in the asoe-ui browser-e2e
job which spins this backend up as a sibling worktree (see
`asoe-ui/.github/workflows/browser-e2e.yml`).

## Required gates

### 1. Bug-fix PRs must include a regression test

Every bug-fix PR MUST include a test that fails on the parent
commit. Verify by:

```bash
git stash
git checkout HEAD~1 -- <fixed-file>
python -m pytest <new-test>      # the test must fail
git checkout HEAD -- <fixed-file>
git stash pop
python -m pytest <new-test>      # the test must now pass
```

Paste both verifications into the PR description. Required for
merge.

This rule applies to ADR-041 P1 (the `case_type` invariants in
`tests/test_case_type_invariants.py` lock the validator behaviour
the bug fix introduced); apply the same pattern to future fixes.

### 2. New `model_validator` requires a focused unit test

Every Pydantic `@model_validator` decorator on
`contracts/models.py`, `api/store.py`, or `api/schemas.py` must
land with a dedicated test class covering:

  * **Each happy path** the validator allows.
  * **Each invariant violation** the validator rejects, with the
    exact `ValidationError` message asserted.

Reference impl: `tests/test_case_type_invariants.py` (15 locks
covering `infer_case_type`, default resolution, EMAIL_ENTRY /
BLOCK invariants, OOV-rejection).

The matching mock-data lock on the asoe-ui side
(`asoe-ui/tests/architectural/case_pivot_mock_wiring.test.ts`)
must land in the same release cycle. Mock drift caused PR #155;
the paired-lock rule prevents recurrence.

### 3. New recipes require a deterministic test path

Recipes are the only place business logic lives (CLAUDE.md
Guardrail #1). Every new recipe lands with:

  * A pytest case that drives `run_graph()` with a constructed
    `OrderEvent` and asserts the recipe is selected + the
    expected effect rows / lifecycle land.
  * **No LLM call.** Constrained-generation outputs must come from
    a deterministic fallback (`constraints/fallback_backend.py`)
    or a stubbed `Outlines` backend.
  * A registry-coverage check: the new recipe appears in
    `recipes/registry.py` and the audit-bearing-registry sweep
    runs against it.

Reference impl: `tests/test_constraints.py` +
`tests/test_recipe_*.py`.

### 4. New WebSocket event types require a UI invalidation pair

Adding a new `case_*` or per-event WS event type in
`api/events.py` requires updating the asoe-ui
`isCaseInvalidationEvent` helper AND the matching architectural
lock. Asymmetry here means a fresh event type silently doesn't
trigger UI refresh.

## Gap-closure patterns

### Pattern A — `model_validator` invariant lock

```python
# tests/test_<entity>_invariants.py
from pydantic import ValidationError

class TestCaseTypeInvariants:
    def test_email_entry_requires_classification(self):
        with pytest.raises(ValidationError, match="email_classification"):
            OrderCase(
                tenant_id="t1",
                source="manual_order",
                source_channel="email",
                source_email_id="msg-001",
                case_type="EMAIL_ENTRY",
                email_classification=None,  # rejected
            )
```

### Pattern B — graph state-transition lock

```python
# tests/test_<intent>_graph.py
def test_block_path_routes_to_recipe(self):
    state = run_graph(seed_block_event())
    assert state["selected_recipe"] == "ExpectedRecipe.py"
    assert state["shadow_verdict"] in {"GREEN", "YELLOW"}
    assert state["final_status"] != "FAIL_TO_HUMAN"
```

### Pattern C — sandbox WebSocket round-trip

```python
# tests/sandbox/test_websocket_<topic>.py
def test_case_open_event_reaches_subscriber(client):
    with client.websocket_connect("/api/v1/ws") as ws:
        client.post("/api/v1/exceptions/resolve", json=seed_email_event())
        msg = ws.receive_json()
        assert msg["type"] == "case_open"
        assert msg["payload"]["case_type"] == "EMAIL_ENTRY"
```

## When you write a new test — checklist

1. **Which layer?** Pick the cheapest layer that catches the
   regression. Pydantic invariant if the fix is a validator. Unit
   if it's a pure function. Graph if it's a state transition.
   Sandbox if it's a route handler. Browser e2e (in asoe-ui) if
   it's an end-to-end contract.
2. **What does it lock?** Write the assertion before the test
   scaffolding. If you can't state the invariant in one sentence,
   the test is wrong.
3. **Does it fail on the buggy version?** Run the verify-failure
   procedure above.
4. **Is it deterministic?** No LLM calls, no real-clock
   dependencies, no flaky timing.

## Reference impls

| Pattern | Reference |
|---|---|
| Pydantic invariant lock | `tests/test_case_type_invariants.py` |
| Detail-path visibility invariant | `tests/test_routes_cases.py::TestDetailVisibilityInvariant` |
| Graph state transition | `tests/test_orchestration_*.py` |
| Recipe registry coverage | `tests/test_audit_registry_coverage.py` |
| WebSocket round-trip | `tests/sandbox/test_websocket_events.py` |
| Constrained-generation lock | `tests/test_constraints.py` |
