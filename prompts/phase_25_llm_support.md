# Phase 25 — Provider-agnostic remote-LLM tier (V1 PR-1)

```text
Read CLAUDE.md (esp. Guardrail #2 determinism over autonomy +
Guardrail #3 constrained generation), architecture_v4.md
(current; §4 LLM tier + §5.3 cross-check + §11 audit governance
of LLMProvenance), tasks.md (Phase 25), DESIGN.md (§1 module map,
§2 backend chain, §9 observability, §12 env-var reference, §19
test coverage), and llm/provider_protocol.py.

Implement only Phase 25 (the per-task, provider-agnostic
remote-LLM tier and the SOX-grade telemetry that lets it run
under audit). This is a retroactive prompt — Phase 25 has
already shipped on branch `claude/add-llm-support-h2t9i` and
been merged. Sub-tasks 25.1-25.8 are CLOSED. The prompt is
recorded here so future sessions reading the sequence can
reconstruct the architectural intent without re-deriving it from
the diff.

Default behaviour after this phase ships is unchanged:
`ASOE_LLM_PROVIDER=fallback` makes every trio call
(classify_intent / propose_recipe / shadow_decision) deterministic.
An operator opts a tenant / environment / task / call into a
remote provider via env vars — never via code change.

Requirements:

25.1 — Provider abstraction (S3a + S3d + S3e):
  * llm/provider_protocol.py — LLMProviderClient Protocol +
    ToolCallResult / SystemBlock / CacheControl / TokenUsage /
    ProviderError dataclasses. The constraints layer sees only
    this protocol — no vendor SDK leaks upward through any import
    or type signature.
  * llm/anthropic_client.py — direct + Foundry. Default
    `claude-sonnet-4-6`. Tool-use forced via
    tool_choice={"type":"tool","name":...}. Cache_control
    ephemeral on cacheable system blocks (system + tools prefix).
    400-with-credit-balance routes to kind='billing'
    (non-retryable; sticky until credits added) — separate
    classification from kind='schema_mismatch' so operators
    triage correctly.
  * llm/openai_client.py — full OpenAI / Azure OpenAI /
    OpenAI-compatible (vLLM / TGI / LiteLLM / LocalAI). Auto-
    detects Azure when OPENAI_API_VERSION is set. Surfaces
    prompt_tokens_details.cached_tokens (OpenAI auto-caching).
  * llm/ollama_client.py — full self-hosted + Cloud. OpenAI-
    style tool calling on Qwen2.5+, Llama 3.1+, Mistral.
  * llm/huggingface_client.py — full HF Dedicated Inference
    Endpoints + Serverless Inference API (production blocks the
    latter; only Dedicated is reachable from prod egress).
  * llm/google_client.py — V1 stub. Vertex AI / Gemini wiring
    deferred to a follow-up phase.
  * llm/provider_factory.py — PROVIDER_FACTORIES registry +
    build_provider_client(provider). Single entry point.
  * Per-provider env-var prefix: ANTHROPIC_* / OPENAI_* /
    OLLAMA_* / HUGGINGFACE_* / GOOGLE_*.
    RemoteLLMConfig.from_env(provider="...") builds the typed
    config from those.
  * Production-egress allowlists: api.anthropic.com /
    api.openai.com / public Ollama Cloud / HF Serverless
    Inference / public Gemini are all blocked when
    ASOE_ENV=production. Only operator-allow-listed Dedicated
    endpoints reach external.
  * Lazy SDK imports — every client module is importable
    without its provider's package; the SDK only loads inside
    from_config(). The core service runs without any vendor SDK
    when the deterministic backend is active.

25.2 — LLM utilities (S2):
  * llm/sanitizer.py — OrderEvent.metadata allowlist +
    length-cap (256 chars) + control-char scrub +
    untrusted-data delimiter. Closes the prompt-injection
    surface flagged in the security & compliance review (Chen
    review §5).
  * llm/budget.py — InMemoryBudgetTracker + RedisBudgetTracker.
    Daily USD spend cap with soft-warn (80%) / hard-block
    (100%) thresholds. Redis errors degrade safely (treat as
    "no budget known" → allow + log, do not crash the call).
  * llm/circuit_breaker.py — LLM-tier breaker. Sliding 60s
    window. Trips on error-rate > 25% OR p95 latency > 15s.
    5-minute cooldown. HALF_OPEN probe with `_probe_in_flight`
    flag so only one call probes at a time.

25.3 — Constraint-layer integration (S3b + S3c + S4):
  * constraints/llm_backend.py — RemoteLLMBackend composes any
    LLMProviderClient + sanitiser + breaker + budget. Exposes
    the trio surface (classify_intent / propose_recipe /
    shadow_decision). Falls through to deterministic on every
    failure mode (provider error, breaker open, budget block,
    validation failure) — never raises into the graph.
  * constraints/router.py — get_constrained_backend(task) with
    full per-task routing precedence:
      1. ASOE_KILL_SWITCH active → deterministic, zero TCP egress
      2. ASOE_EXPLAIN_MODE active → deterministic, read-only
      3. ASOE_LLM_DISABLE_FOR={task} → deterministic for that task
      4. ASOE_LLM_PROVIDER_<TASK> per-task override
      5. ASOE_LLM_PROVIDER global default
      6. USE_OUTLINES_BACKEND=1 legacy short-circuit
      7. fallback (deterministic)
  * constraints/cross_check.py — pure-function comparator;
    orchestration `classify` runs the deterministic classifier
    in parallel with the LLM call and routes to
    MANUAL_REVIEW_REQUIRED on disagreement (architecture_v4.md
    §4 v4 amendment).
  * tests/test_llm_cache_invalidators.py — byte-identical
    system+tools prefix audit. Panel-blocked CI guard against
    cache-hit-rate regressions; touching the system prompt
    silently invalidates every operator's cache, so the hash
    must be reviewed deliberately.

25.4 — Orchestration wiring (S4):
  * orchestration/nodes.py — per-task `_backend(task)` cache;
    `classify` → task='intent', `select_recipe` → task='recipe',
    `shadow_audit` → task='shadow'. Cross-check inline on the
    classify path. Drains `last_call_trace` onto state after
    each call so the trace propagates downstream without the
    nodes knowing about the backend's internal bookkeeping.
  * compliance/shadow.py — default backend uses
    get_constrained_backend(task='shadow'). LLM provider for
    shadow gets enabled only after compliance sign-off (the
    operator-tunable knob for that is
    ASOE_LLM_PROVIDER_SHADOW).
  * Kill-switch + explain-mode pinning verified end-to-end —
    no provider client is constructed when either gate is
    active. The router short-circuits before any vendor SDK
    import attempts to load.

25.5 — SOX-grade telemetry (S5a + S5b):
  * contracts/models.py — LLMCallTrace Pydantic model with:
    provider / model_id / request_id / token usage
    (input/output/cache_read/cache_creation) / latency_ms /
    cost_usd_estimate / stop_reason / fallback_to_deterministic
    / fallback_reason / cross_check_disagreement /
    cross_check_llm_intent / cross_check_deterministic_intent /
    skill_md_version / prompt_hash / tool_call_hash.
    GraphState.llm_call_traces: List[LLMCallTrace].
  * RemoteLLMBackend.last_call_trace populated at every
    `_invoke` exit branch — success, ProviderError,
    CircuitOpen, budget hard-block, validation error. SHA-256
    `prompt_hash` + `tool_call_hash` + `skill_md_version` are
    written for cross-pod reproducibility audits.
    **Prompt and tool-call content are never logged — only the
    hashes of them.** Operators can prove "the same prompt was
    sent twice" without exposing what was in the prompt.
  * observability/tracer.py — TraceRecord.llm_calls +
    aggregate scalars (token totals, cost, fallback flag,
    disagreement flag). The aggregates make Prometheus + audit
    queries cheap; the per-call detail is in `llm_calls`.
  * observability/langfuse_sink.py — emits one
    `generation`-typed observation per LLMCallTrace on both
    LangFuse v2 (trace/span) and v4 (start_observation) SDK
    paths. Native LangFuse fields (model, usage); audit /
    fallback / cross-check signals in metadata. Prompt content
    NEVER forwarded — only hashes.

25.6 — Compliance audit registry (S5c):
  * compliance/audit_bearing_registry.yaml — new LLMProvenance
    section with 3 audit-bearing rows:
      - llm_provider_used    (which vendor served the call)
      - llm_model_id         (exact model snapshot)
      - llm_request_id       (vendor-side request_id for replay)
    pending_signoff: true until the workshop follow-up flips
    it to false. Summary tally updated 107 → 110, 82 → 85.
  * Until sign-off lands, the rows ARE in the registry but the
    composer's coverage check treats them as advisory. Post-
    sign-off the flag flips and missing values route to
    AUDIT_CONTEXT_MISSING.

25.7 — Docs (S5d):
  * DESIGN.md — §1 module map (the new llm/ package), §2
    backend chain (router → RemoteLLMBackend → provider),
    §9 observability (trace shape + langfuse mapping),
    §12 env-var reference (full provider matrix),
    §19 test coverage.
  * architecture_v3.md — §5.3 per-task router + provider
    matrix + cross-check + cost guardrails; §18 env vars.
    (Subsequently superseded by architecture_v4.md which
    absorbs §5.3 and adds the §4 cross-check amendment.)
  * .env.example — full provider env-var inventory with
    annotations for every variable.
  * pyproject.toml — [anthropic, openai, ollama, huggingface]
    optional dependency groups. Operator only installs the
    SDKs for providers they actually use.

25.8 — Test deltas:
  +249 net new tests across S2 / S3 / S4 / S5. Final suite:
  1592 passed, 35 skipped (vs Phase 24 baseline 1343 passed,
  35 skipped). Zero regressions.

  All provider tests are network-free — they monkeypatch
  sys.modules with stub SDK objects so the SDK never has to
  load. CI does not need vendor API keys.

  The golden-path graph tests still pass with the default
  ASOE_LLM_PROVIDER=fallback. Operators can run the entire
  suite locally without any LLM credentials.

Definition of done:
  * Default behaviour unchanged — `pytest -q` is green with no
    LLM env vars set.
  * Every trio call is interceptable per task per environment
    via env vars — no code change required to flip a tenant's
    classifier from deterministic to Anthropic.
  * Every LLM call emits an LLMCallTrace with the audit fields
    populated, including failure paths (the trace records
    `fallback_to_deterministic=True` + `fallback_reason` so the
    operator can see WHY the LLM was bypassed).
  * Cross-check runs deterministically on the `intent` path;
    disagreements flag `cross_check_disagreement=True` and
    route to MANUAL_REVIEW_REQUIRED.
  * Production egress is closed for every provider that hosts
    a public Serverless surface; only operator-allow-listed
    Dedicated endpoints reach external.
  * Kill-switch and explain-mode preserve their absolute
    semantics — no provider client is constructed when either
    is active.
  * LLMProvenance compliance rows are in the registry awaiting
    sign-off; production rollout requires the sign-off flag
    flip plus the operator's per-tenant provider config.

Do NOT:
  * Change the default to a non-fallback provider. The hardening
    panel explicitly chose fallback as the safe default.
  * Add a provider whose Serverless surface is not blocked in
    production. New providers MUST land in the prod egress
    allowlist before merging.
  * Remove the cross-check on the intent path. The deterministic
    classifier is the backstop for LLM drift; keeping it cheap
    means it always runs.
  * Log prompt content or tool-call content. Hashes only —
    `prompt_hash` and `tool_call_hash` provide replay-ability
    without exposing payload.
  * Conflate `kind='billing'` with `kind='schema_mismatch'`.
    Billing is sticky and operator-actionable (top up credits);
    schema_mismatch is a payload bug. Routing them through the
    same fallback_reason hides the difference and breaks
    operator triage.
  * Move shadow-task LLM enablement out of operator-tunable env
    space. ASOE_LLM_PROVIDER_SHADOW gates the compliance-
    sensitive path and must remain explicitly opt-in until the
    LLMProvenance audit registry sign-off lands.
  * Re-introduce a global "use LLM everywhere" knob. Routing is
    per-task by design — the `intent`, `recipe`, and `shadow`
    surfaces have different cost / latency / risk profiles.

Return:
  identified intent: provider-agnostic remote-LLM tier
  selected skill: n/a (infrastructure work, not classifier
    behaviour change)
  selected recipe: n/a (no recipe execution touched)
  Compliance Shadow result: unchanged (shadow LLM tier requires
    LLMProvenance sign-off before activation)
  deterministic execution log or halt reason: see test summary —
    1592 passed, 35 skipped; +249 net new tests; zero regressions
```

## See also

- `tasks.md` Phase 25 — task-by-task ledger with PR / branch
  cross-references.
- `architecture_v4.md` §4 (LLM tier) and §11 (LLMProvenance
  audit governance) — the architectural commitments this phase
  realises.
- `DESIGN.md` §1 module map / §2 backend chain / §9
  observability / §12 env-var reference / §19 test coverage —
  the operator-level documentation surface.
- `.env.example` — full provider env-var inventory.
- `compliance/audit_bearing_registry.yaml` — LLMProvenance
  rows (`pending_signoff: true` until workshop follow-up).
- `llm/provider_protocol.py` — the contract the constraints
  layer depends on; the only abstraction the rest of the
  codebase sees.
- `prompts/phase_26_post_deploy_fixes.md` — the immediately
  following phase, which references "Phase 25 baseline 1592
  passed" in its test-delta accounting.
