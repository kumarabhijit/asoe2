# Phase 7b — Multi-Step Workflows (Saga Pattern)

```text
Read architecture_v2.md, DESIGN.md, CLAUDE.md, and tasks.md (Phase 7.2).
Implement only Phase 7.2. Phase 7a (gateways) must already be complete.

Requirements:
- add typed contracts: WorkflowStep, WorkflowDefinition, WorkflowStepResult, WorkflowResult
- implement WorkflowRunner.run() — sequential step execution through the full graph
- each step runs its own independent Compliance Shadow audit
- on step failure: invoke compensation_event for completed steps in LIFO order
- WorkflowResult.status: COMPLETE, FAILED, COMPENSATED, PARTIAL
- support input_mapping to carry state forward between steps

Constraints:
- WorkflowRunner must call run_graph() — no bypass of shadow, circuit breaker, or recipe validation
- all new contracts use extra="forbid"
- do not add speculative features beyond tasks.md

Add tests for: COMPLETE path, FAILED path (no compensation), COMPENSATED path
(LIFO compensation order), independent shadow audit per step.
```
