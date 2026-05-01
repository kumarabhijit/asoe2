# Phase 26 — Post-deploy fixes + operational hardening

```text
Read CLAUDE.md (esp. Guardrail #6 — UI richness as product
commitment + partial-truth veto), architecture_v4.md (current),
tasks.md (Phase 25), docs/adr/ADR-026-event-driven-ingestion.md,
and docs/adr/ADR-027-pipeline-visualization-hybrid.md (Proposed
status; rev. 3 reflects review-board amendments).

Implement only Phase 26 (post-deploy stabilisation that landed
after architecture_v3.md was finalised). v4 absorbs the shipped
items from this phase; ADR-026 and ADR-027 remain Proposed for
v4.1 absorption.

This is a retroactive prompt — Phase 26 has already shipped on
core_ui_integration. Sub-tasks 26.1-26.3 are CLOSED; 26.4 is two
ADR drafts, both Proposed; 26.5 is the test-suite delta.

Requirements:

26.1 — Env-driven JWT TTLs (operator-tunable):
  * api/deps.py::_resolve_token_ttls — pure function reading
    ASOE_ACCESS_TOKEN_TTL_SECONDS / ASOE_REFRESH_TOKEN_TTL_SECONDS.
    Defaults: sandbox 24h access / 30d refresh; production
    60min access / 7d refresh.
  * Defensive against empty / malformed / zero / negative — fall
    back to per-environment default rather than crash. Inner
    helper:

      def _ttl_or_default(env_var: str, default: int) -> int:
          raw = os.getenv(env_var, "").strip()
          if not raw: return default
          try: value = int(raw)
          except ValueError: return default
          return value if value > 0 else default

  * infra/main.bicep — accessTokenTtlSeconds /
    refreshTokenTtlSeconds @secure() string params, wired as
    container env vars. Operator presets documented:
    900 (15min) / 3600 (1h) / 86400 (24h).
  * Tests: empty string, malformed int, zero, negative, env-driven
    happy path. (api/tests/test_deps.py)

26.2 — Confidence persistence + scaled read (Verdict 2026-04-22 /
       Guardrail #6):
  * api/routes/exceptions.py /resolve write site (~L229) —
    persist intent_confidence: state.confidence (0.0-1.0 float)
    into trace_data alongside intent_selected.
  * api/routes/exceptions.py /reanalyze write site (~L1245) —
    same persistence so reanalysed records carry the new attempt's
    classifier confidence.
  * api/routes/exceptions.py read path (~L1501) —
    AnalysisResponse.confidence scales from the persisted 0.0-1.0
    float to 0-100 int with max(0, min(100, int(round(raw * 100))))
    clamp. Missing / zero / negative returns 0 — never a fabricated
    mid-range default.

      raw_conf = trace_data.get("intent_confidence")
      if isinstance(raw_conf, (int, float)) and raw_conf > 0:
          confidence = max(0, min(100, int(round(raw_conf * 100))))
      else:
          confidence = 0

  * Remove the legacy fabricated 70 in the no-trace branch (was
    "confidence = 70 if record.intent else 0"; now stays at 0).
  * Tests: tests/test_analysis_confidence_persistence.py — 4 cases:
    - fallback-classifier confidence (0.90 → 90 for
      CONTRACTUAL_CORRECTION)
    - no-trace records → 0 (not the fabricated 70)
    - malformed string trace ("not_a_number") → 0
    - out-of-range (1.5) → clamped to 100

  * Closes the deployed-system "every record at 80%" partial-
    truth state; the legacy hardcode was
    `confidence = 80 if intent_selected else 0`.

26.3 — V005 — drop intent CHECK constraint + UUID/datetime coercion:
  * db/migrations/V005__drop_intent_check.sql — drops
    chk_exceptions_intent. Intent vocabulary now lives exclusively
    in contracts/models.py::Intent; adding a new intent requires
    zero DB migration coordination.
  * db/repository.py row-to-dict — UUID and datetime values
    coerced to strings on read so JSON serialisation does not
    crash on Postgres native types.

26.4 — ADR drafts (Proposed; not yet shipped — DO NOT implement):
  * ADR-026 — Event-driven ingestion via Azure Event Hubs (Phase B).
    Distinguishes ingestion (push, async, bus) from enrichment
    (pull, sync, gateway). Phase B.2 documents per-node real
    WaterfallStepper timings as deferred (orchestrator emission gap
    — WSEvent.pipeline_progress factory exists but is uncalled).
  * ADR-027 — Pipeline visualization hybrid (rev. 3 — reanalysis
    attempt-scoping). Replaces WaterfallStepper with two trace-
    derived surfaces: operator-first EventsTimeline + audit-first
    PipelineDAG. Topology comes from a new
    GET /api/v1/pipeline/topology endpoint introspecting
    compiled_graph.get_graph(). Each reanalysis attempt's
    executed_nodes list is preserved on a typed
    ReanalysisHistoryEntry (currently List[Dict[str, Any]]).
    Reviewer chain: AI/LangGraph → Compliance → Tools Admin →
    Frontend Platform → Compliance veto holder. Estimated 8-9 days
    end-to-end.

  Both ADRs require review-board sign-off before implementation.
  Phase 26 commits the drafts so the review chain can run; the
  implementation is queued for separate phases (Phase 27 / 28
  numbering TBD when the ADRs are ratified).

26.5 — Tests:
  Final suite: 1688 passed, 35 skipped (vs Phase 25 baseline 1592
  passed, 35 skipped). +96 net new tests across 26.1 / 26.2 / 26.3
  plus the asoe-ui-side architectural lock test asserting every
  LIVE_METHODS entry has its `if (USE_REAL_API)` branch (covered
  in asoe-ui Phase 8.13).

Output:
1. List affected files
2. Show the env-driven TTL pure function with all defensive branches
3. Show the persisted intent_confidence dict shape at both write sites
4. Show the scaled read-path with the clamp + missing-value fallback
5. Test summary: which 4 confidence tests + which TTL tests pass

Do NOT:
- Implement ADR-026 (event-driven ingestion) — it is Proposed
- Implement ADR-027 (pipeline visualization hybrid) — it is Proposed
- Resurrect the fabricated 80 / 70 confidence defaults
- Re-add the chk_exceptions_intent CHECK constraint
- Add a UI fallback chain for confidence display — UI is a dumb
  projector, the backend is the source of truth (Pillar 3 / Verdict
  2026-04-22)

Return:
  identified intent: post-deploy stabilisation
  selected skill: n/a (operational hardening, not classifier work)
  selected recipe: n/a (no recipe execution touched)
  Compliance Shadow result: n/a (orchestration unaffected)
  deterministic execution log or halt reason: see test summary
```

---

## Notes for future sessions

This phase is the bridge between architecture_v3 (Apr 26) and
architecture_v4 (May 1). The shipped items (26.1-26.3) are the
tactical fixes that the deployed sandbox needed; the ADR drafts
(26.4) are the architectural follow-ups that the review board
will decide on.

A reader picking up Phase 27 or 28 should:
1. Confirm both ADRs have been ratified by the review board chain.
2. Cross-reference architecture_v4.md §13 — when ADR-026 and
   ADR-027 land, v4.1 absorbs them. The §13 "Proposed" pointers
   retire; the §11 audit-governance section gains the
   executed_nodes evidence shape; §12 UI cross-reference points at
   the new EventsTimeline + DAG surfaces.
3. Plan Phase 27 (event-driven ingestion implementation) against
   ADR-026's connector + bus consumer architecture.
4. Plan Phase 28 (pipeline visualization implementation) against
   ADR-027's phase ladder (A.0 verdict-vocabulary registration →
   A topology endpoint → B trace extension + WS batching → C
   EventsTimeline → D PipelineDAG → E role-based default).
