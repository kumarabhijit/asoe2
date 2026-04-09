# Phase 10 — LangFuse Observability Integration

```text
Read architecture_v3.md, CLAUDE.md, DESIGN.md, and tasks.md (Phase 10)
before making any changes.  Also read observability/tracer.py and
observability/langfuse_sink.py in full.

Goal: forward structured trace records to LangFuse so engineers and
auditors can inspect pipeline executions in the LangFuse dashboard —
without disrupting the existing stdlib-logging audit trail.

────────────────────────────────────────────────────────────────────────
DESIGN PRINCIPLES
────────────────────────────────────────────────────────────────────────

1. Optional dependency — langfuse is an optional pip extra.
   CI/tests must never require it.  The sink is a no-op when the
   package is missing or env vars are unset.

2. Stdlib logging stays primary — it is the authoritative audit
   record.  LangFuse is additive.

3. Failure isolation — all LangFuse errors are caught and logged.
   Forwarding failures never block graph execution.

4. SDK version compatibility — the sink auto-detects langfuse v2
   (trace/span/score) vs v4+ (start_observation/create_score) and
   uses the correct API.

5. Secrets via Key Vault — LANGFUSE_PUBLIC_KEY and
   LANGFUSE_SECRET_KEY are managed through Azure Key Vault CSI in
   production, env vars in dev.

────────────────────────────────────────────────────────────────────────
COMPONENTS
────────────────────────────────────────────────────────────────────────

1. observability/langfuse_sink.py
   - _get_client()    Lazy-init; reads LANGFUSE_PUBLIC_KEY,
                      LANGFUSE_SECRET_KEY, LANGFUSE_HOST.
   - forward(record)  Maps TraceRecord → LangFuse trace + spans
                      (classify, load_skill, shadow_audit,
                      execute_recipe) + terminal_status score.
   - flush()          Explicit flush for short-lived processes.
   - reset_client()   Test helper for re-initialisation.
   - _is_v2(client)   Detects SDK version (v2 vs v4+).

2. observability/tracer.py — Tracer.emit()
   - After emitting the stdlib JSON log, calls
     langfuse_sink.forward(record) inside a try/except.
   - Import is lazy (inside emit()); no module-level langfuse dep.

3. pyproject.toml
   - [project.optional-dependencies] langfuse = ["langfuse>=2.0.0"]

4. Container images
   - Dockerfile.core and Dockerfile.ui include langfuse>=2.0.0.
   - Dockerfile.inference is unchanged (no observability module).

5. docker-compose.yml
   - LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST added
     to the shared x-core-env block.

6. k8s/core/secret-provider.yaml
   - langfuse-public-key and langfuse-secret-key synced from
     Azure Key Vault to the asoe-secrets Kubernetes Secret.

7. Sandbox integration
   - CLI runner (tests/sandbox/cli.py): --langfuse-flush flag,
     LangFuse status in env banner.
   - Streamlit UI (tests/sandbox/ui/app.py): LangFuse status in
     environment expander.
   - requirements-sandbox.txt: langfuse>=2.0.0 as commented
     optional dep.

────────────────────────────────────────────────────────────────────────
LANGFUSE TRACE MAPPING
────────────────────────────────────────────────────────────────────────

Each run_graph() call produces one LangFuse trace:

  LangFuse entity         ASOE source
  ─────────────────────── ─────────────────────────────────────
  trace.id                TraceRecord.trace_id
  trace.name              "asoe-graph-execution"
  trace.input             { event_id }
  trace.output            { final_status, explanation }
  trace.metadata          { constrained_output_schemas,
                            gateway_calls, rag_chunks,
                            resolved_by, resolved_action,
                            resolution_notes }
  span "classify"         intent_selected
  span "load_skill"       skill_name
  span "shadow_audit"     shadow_verdict, shadow_policy_hits
                          (level=WARNING if non-GREEN)
  span "execute_recipe"   recipe_name
  score "terminal_status" 1.0 if COMPLETE, 0.0 otherwise

  Override audit fields (Phase 11.5):
    resolved_by, resolved_action, resolution_notes are included in
    trace.metadata when present (non-None).  These capture human
    overrides of agent recommendations for compliance audit.

terminal_status score values:

  final_status              value   meaning
  ───────────────────────── ─────── ─────────────────────────────────
  COMPLETE                  1.0     Recipe executed successfully
  FAIL_TO_HUMAN             0.0     Escalated to human
  MANUAL_REVIEW_REQUIRED    0.0     Shadow YELLOW — requires review
  BLOCKED                   0.0     Shadow RED — halted by policy
  REJECTED                  0.0     Rejected by policy

The comment field on the score contains the exact final_status string,
enabling root-cause filtering in the LangFuse dashboard.

────────────────────────────────────────────────────────────────────────
TESTS
────────────────────────────────────────────────────────────────────────

All LangFuse tests live in tests/test_observability.py:

  TestLangFuseSinkDisabled
  - No-op when keys not set, package missing, keys empty.

  TestLangFuseSinkWithMockClient
  - Trace/span/score creation with injected mock client.
  - Shadow audit WARNING level on non-GREEN verdict.
  - DEFAULT level on GREEN.
  - Score 1.0 on COMPLETE, 0.0 on FAIL_TO_HUMAN.
  - No child spans when optional fields are None.
  - Exception isolation (client raises → returns False).

  TestTracerEmitWithLangFuse
  - Dual-emit: stdlib log + LangFuse forward.
  - Sink failure does not block stdlib emit.

All tests are network-free.  Mock client is injected directly
into the sink module globals.

Run:
  python -m pytest tests/test_observability.py -v
  python -m pytest tests/test_observability.py -v -k "LangFuse"

────────────────────────────────────────────────────────────────────────
SELF-HOSTED LANGFUSE SETUP (without Docker)
────────────────────────────────────────────────────────────────────────

For local testing against a real LangFuse server:

  # 1. Start PostgreSQL
  pg_ctlcluster 16 main start

  # 2. Create database
  sudo -u postgres psql -c "CREATE USER langfuse WITH PASSWORD 'langfuse' CREATEDB;"
  sudo -u postgres psql -c "CREATE DATABASE langfuse OWNER langfuse;"

  # 3. Clone LangFuse v2 (Postgres-only, no ClickHouse required)
  git clone --depth 1 --branch v2.95.1 https://github.com/langfuse/langfuse.git
  cd langfuse

  # 4. Configure .env (DATABASE_URL, DIRECT_URL, NEXTAUTH_SECRET,
  #    SALT, ENCRYPTION_KEY, LANGFUSE_INIT_* for auto-provisioning)

  # 5. Install, migrate, build, start
  pnpm install --no-frozen-lockfile
  pnpm --filter=shared run db:migrate
  pnpm run build
  pnpm --filter=web run start

  # 6. Health check
  curl http://localhost:3000/api/public/health
  # → {"status":"OK","version":"2.95.1"}

  # 7. Run ASOE sandbox with LangFuse
  LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
    LANGFUSE_HOST=http://localhost:3000 \
    PYTHONPATH=. python tests/sandbox/cli.py --langfuse-flush

  # 8. Verify traces via API
  curl -u pk-lf-...:sk-lf-... http://localhost:3000/api/public/traces

Note: LangFuse v3.x+ requires ClickHouse and S3.  For local dev
use v2.95.1 which only needs PostgreSQL.

────────────────────────────────────────────────────────────────────────
CONSTRAINTS
────────────────────────────────────────────────────────────────────────

- langfuse_sink.py must not import langfuse at module level.
- Tracer.emit() must not raise if langfuse_sink.forward() fails.
- No production module (contracts/, orchestration/, compliance/,
  recipes/) may import from observability/langfuse_sink.py.
- CI tests must pass without the langfuse package installed.
- LangFuse keys must never appear in source code, Dockerfiles,
  or env var defaults.

────────────────────────────────────────────────────────────────────────
DEFINITION OF DONE
────────────────────────────────────────────────────────────────────────

- [ ] langfuse_sink.py forwards TraceRecord to LangFuse when configured
- [ ] Tracer.emit() calls forward() with full error isolation
- [ ] langfuse is an optional dependency (pyproject.toml, Dockerfiles)
- [ ] Env vars documented (.env.example, DESIGN.md, README.md)
- [ ] k8s secrets wired (secret-provider.yaml)
- [ ] Sandbox tools show LangFuse status and support --langfuse-flush
- [ ] Tests cover disabled, mock, and failure paths (network-free)
- [ ] python -m pytest passes — all tests pass, zero failures
- [ ] Docs updated (README, AUDITOR_GUIDE, tasks.md, DESIGN.md)
```
