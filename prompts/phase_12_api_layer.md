# Phase 12 — FastAPI API Layer

```text
Read architecture_v3.md §8 (API Contract), §11.1–11.3 (Auth, RBAC, Multi-Tenancy),
CLAUDE.md, DESIGN.md §15, and tasks.md (Phase 12).
Implement only Phase 12.

Requirements:
- create api/ module with FastAPI application factory (api/app.py)
- add standard error envelope (api/errors.py) per architecture_v3.md §8.3:
  { "error": { "code", "message", "trace_id", "details" } }
- add request/response Pydantic models (api/schemas.py) for all endpoints
- add in-memory exception store (api/store.py) — replaced by PostgreSQL in Phase 13
- implement JWT auth dependency (api/deps.py):
  - extract Bearer token from Authorization header
  - validate signature (HS256 with configurable secret)
  - extract claims: sub, email, name, roles[], org (tenant_id), env, permissions[]
- implement RBAC dependency factory (require_role(*roles)):
  - 5 roles: analyst, manager, admin, viewer, partner
  - permissions: {resource}:{action} pattern per §11.2
- implement tenant extraction (get_tenant_id()) from JWT org claim per §11.3

Endpoints (19 routes):
- GET  /api/v1/health                           — public; return status, version, kill_switch, explain_mode,
                                                   allowed_intents, lifecycle_states, allowed_recipes (Guardrail #2)
- POST /api/v1/exceptions/resolve               — analyst+; construct OrderEvent, run_graph(), persist, return result
- POST /api/v1/exceptions/resolve/async          — analyst+; V1 stub (runs synchronously, returns task_id)
- POST /api/v1/exceptions/resolve/explain        — analyst+; force ASOE_EXPLAIN_MODE=1 for this request
- GET  /api/v1/exceptions                        — analyst+; paginated list (cursor-based, filter by status/intent)
- GET  /api/v1/exceptions/stats                  — analyst+; dashboard metrics (total, open, resolved, blocked, failed)
- GET  /api/v1/exceptions/{id}                   — analyst+; exception detail
- GET  /api/v1/exceptions/{id}/trace             — analyst+; full TraceRecord JSON
- PATCH /api/v1/exceptions/{id}/override         — manager+; human override (action, notes, resolved_by)
- POST  /api/v1/exceptions/{id}/approve          — manager+; resume PENDING_REVIEW exception
- POST  /api/v1/exceptions/{id}/reject           — manager+; reject PENDING_REVIEW exception
- POST /api/v1/workflows                         — manager+; WorkflowDefinition + events → WorkflowResult
- PUT  /api/v1/policies/{tenant_id}              — admin; update tenant-specific policy override
- POST /api/auth/login                           — public; email/password → MFA challenge (admin-only, MFA enforced)
- POST /api/auth/sso/init                        — public; SSO initiation (stub — returns redirect URL)
- GET  /api/auth/sso/callback                    — public; SSO callback (stub — returns test tokens)
- POST /api/auth/mfa/verify                      — public; MFA verification (stub — issues tokens)
- POST /api/auth/refresh                         — public; validate refresh token, issue rotated tokens
- GET  /api/auth/me                              — any; current user profile from JWT claims

Constraints:
- resolve endpoints call run_graph() from orchestration/graph.py — no new execution logic
- all errors use the standard error envelope (ASOEError → ErrorEnvelope)
- tenant_id scoping on all queries (application-layer isolation)
- no changes to the core engine (contracts/, orchestration/, recipes/, etc.)
- do not add speculative features beyond architecture_v3.md §8

Add tests for: health (dynamic enums), auth (missing/invalid/valid token),
RBAC (viewer cannot resolve, analyst cannot override, admin can update policy),
tenant isolation (tenant-a cannot see tenant-b), resolve (sync, async, explain),
CRUD (list, detail, trace, pagination), override, approve/reject (state validation),
workflows (valid + invalid intent), policies, all auth endpoints, error envelope format.

Update: DESIGN.md (add §15 API layer), tasks.md (Phase 12 checklist),
sandbox CLI and UI (API server status in environment banner).
```
