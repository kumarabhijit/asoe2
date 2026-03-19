# Phase 4 — LangGraph
```text
Implement only PHASE 4 from tasks.md.
Build an explicit, loop-safe LangGraph state machine.
States should support:
- Ingest
- Classify
- Load Skill
- Shadow Audit
- Validate Types
- Execute Recipe
- Fail to Human
- Complete
Requirements:
- no hidden loops
- deterministic state transitions only
- route to HITL / terminal state on circuit-breaker breach
- preserve Guidance / Outlines boundaries for machine-consumed outputs
Add tests for happy path and escalation paths.
```
