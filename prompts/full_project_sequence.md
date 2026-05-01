# Full Project Sequence Prompt
```text
Read architecture_v4.md (current; v3 superseded 2026-05-01 — read v3 only for foundational sections v4 defers to), DESIGN.md, CLAUDE.md, the sample recipes, the sample skill, and tasks.md.
Work phase-by-phase only.
For the current response:
1. identify the exact phase being implemented
2. list affected files
3. implement the smallest viable increment
4. add tests first or together with code
5. preserve deterministic architecture
6. never invent new business logic outside recipes
7. never bypass Compliance Shadow
8. use Guidance / Outlines for all machine-consumed LLM outputs
9. stop and escalate when deterministic execution is not possible
Return a concise execution summary and test summary.

Phase sequence and build prompts:
  0   — Foundation (contracts, repo structure)         → prompts/phase_0_foundation.md
  1   — Skill Loading & Reasoning                      → prompts/phase_1_skill_reasoning.md
  2   — Compliance Shadow                              → prompts/phase_2_compliance_shadow.md
  3   — Recipe Invocation                              → prompts/phase_3_recipe_invocation.md
  4   — LangGraph Orchestration                        → prompts/phase_4_langgraph.md
  5   — Observability & Tests                          → prompts/phase_5_observability_tests.md
  6   — Hardening (kill switch, explain mode)           → prompts/phase_6_hardening.md
  7a  — Infrastructure Gateways                        → prompts/phase_7a_gateways.md
  7b  — Multi-Step Workflows (Saga)                    → prompts/phase_7b_workflows.md
  8   — Local Execution Sandbox                        → prompts/phase_8_sandbox.md
  9   — Containerized Deployment                       → prompts/phase_9_containerized_deployment.md
  10  — LangFuse Observability Integration             → prompts/phase_10_langfuse.md
  11  — Duplicate PO Product Spec Gap Closure          → prompts/phase_11_duplicate_po_enhancements.md
  12  — FastAPI API Layer                              → prompts/phase_12_api_layer.md
  13  — Database Layer (PostgreSQL Schema & Migrations) → prompts/phase_13_database_layer.md
  14  — Auth & Security Hardening                      → prompts/phase_14_auth_security.md
  15  — WebSocket / Redis Real-Time Event Publishing   → prompts/phase_15_websocket_redis.md
  16  — V1 Foundation Guardrail Tests (CI Enforcement) → prompts/phase_16_v1_guardrails.md
  17  — superseded — see tasks.md history.
  18  — Server-Side User Profiles & Account Entity     → see tasks.md PHASE 18
  19  — Override Action Consolidation (Option A)        → see tasks.md PHASE 19
  20  — Hash-Chained Append-Only Audit Log              → see tasks.md PHASE 20
  21  — OM Coverage: PRICE_HOLD_RELEASE + EDI_MISMATCH  → see tasks.md PHASE 21
  22  — UI Intent Parity (BACK_ORDER / OVER_MAX / MIN_ORDER_QTY / PALLET_CONFIG / DELIVERY_DELAY) → see tasks.md PHASE 22
  23  — Verdict three-pillar architecture              → see tasks.md PHASE 23
  24  — Verdict Full-Close (retire all grandfather clauses + ADR-025 graph reorder) → prompts/phase_24_verdict_full_close.md
  25  — Provider-agnostic remote-LLM tier + LLM/deterministic cross-check + LLMProvenance audit registry → prompts/phase_25_llm_support.md
  26  — Post-deploy fixes + operational hardening (env-driven JWT TTLs, real classifier confidence persistence, V005 drop intent CHECK constraint, ADR-026/027 drafts) → prompts/phase_26_post_deploy_fixes.md
```

## Architectural Decision Records

ADRs document load-bearing architectural decisions that ripple
across the codebase. Read them as a set when onboarding; they are
the historical record of "why this shape" decisions.

| # | ADR | Status |
|---|---|---|
| 021 | Core deployment model (Container Apps) | Ratified |
| 022 | Database access pattern | Ratified |
| 023 | Disposition + hash-chained audit | Ratified |
| 024 | OM coverage expansion (PRICE_HOLD_RELEASE + EDI_MISMATCH) | Ratified |
| 025 | Gateway reads moved before shadow_audit | Ratified — absorbed into architecture_v4.md §5 |
| 026 | Event-driven ingestion via Azure Event Hubs (Phase B) | Proposed — not yet shipped |
| 027 | Pipeline visualization hybrid (rev. 3 — reanalysis attempt-scoping) | Proposed — not yet shipped; reviewer chain AI/LangGraph → Compliance → Tools Admin → Frontend Platform → Compliance veto holder |

## Future architecture revisions

architecture_v4.md (2026-05-01) is the current synthesis. v4.1
absorbs ADR-026 + ADR-027 once they ship + are ratified.
Versioning discipline is documented in architecture_v4.md §14.
