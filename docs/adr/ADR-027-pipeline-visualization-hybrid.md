# ADR-027: Pipeline visualization — hybrid trace-derived timeline + DAG

**Status:** Proposed (rev. 2 — review board amendments applied 2026-05-01)
**Date:** 2026-05-01
**Deciders:** Principal AI Systems Architect; Frontend Platform; AI/LangGraph;
Compliance; ASOE Tools Admin (review pending)
**Review history:**
  - 2026-05-01 rev. 1 — initial draft
  - 2026-05-01 rev. 2 — review-board feedback incorporated (Phase A.0
    verdict-vocabulary registration; ExecutedNode `policy_hits` +
    `timestamp`; fourth canonical trace; `dagre` pinned + 20KB
    ceiling; WS batching; `dashboard:read` dropped to authentication-
    only; real-time timeline append moved into Phase B; estimate
    revised to 7-8 days). One amendment **rejected**: per-execution
    `node_execution_id` for `build_analysis` re-entry —
    reanalysis is captured as a separate `reanalysis_history`
    entry, not as multiple executions on a single trace, so the
    field is unnecessary. See "Amendments after review board" at
    the end of this document for the full delta.
**Applies to:** `asoe-ui/src/components/ui/WaterfallStepper.tsx`,
`asoe-ui/src/app/exceptions/shared.tsx`,
`asoe-ui/src/app/exceptions/DiagnosticsSection.tsx`, new
`asoe2/api/routes/pipeline.py` (or extension to `routes/exceptions.py`),
`asoe2/orchestration/graph.py` (introspection helper),
`asoe2/api/schemas.py` (new `PipelineTopology`, extended trace shape).

---

## Context

The current pipeline view is `WaterfallStepper` — a linear, top-to-bottom
list of 11 nodes (`ingest → classify → … → build_analysis`). The node
list is hardcoded in `asoe-ui/src/app/exceptions/shared.tsx::PIPELINE_NODES`
and lifecycle-to-step mapping is hardcoded in `STATE_PROGRESS`:

```ts
const STATE_PROGRESS: Record<string, number> = {
  INGESTED: 0, CLASSIFYING: 1, AUDITING: 7,
  PENDING_REVIEW: 11, ESCALATED: 11,
  PENDING_ADMIN_REVIEW: 11, PENDING_COSIGN: 11,
  RESOLVED: 11, CLOSED: 11,
  FAILED: 9, BLOCKED: 11, REJECTED: 11,   // ← FAILED→9 is the trip wire
};
```

The graph this is meant to project is **not linear**. Per
`orchestration/graph.py`, every conditional edge (`route_after_gate`)
is a fork that can route the run to `build_analysis` early. There are
five distinct early-exit edges before `execute_recipe` even has a chance
to run:

```
ingest → classify → load_skill → validate_circuit_breaker
  ├─[breach]→ build_analysis (FAIL_TO_HUMAN)
  └─[ok]→ select_recipe
       ├─[no recipe]→ build_analysis (FAIL_TO_HUMAN)
       └─[ok]→ resolve_dependencies
            ├─[required-gw fail]→ build_analysis (FAIL_TO_HUMAN)
            └─[ok]→ validate_types
                 ├─[invocation fail]→ build_analysis
                 └─[ok]→ shadow_audit
                      ├─[RED]→ build_analysis (BLOCKED)
                      ├─[YELLOW]→ build_analysis (MANUAL_REVIEW_REQUIRED)
                      └─[GREEN]→ execute_recipe → apply_effects → build_analysis
```

Plus the classify-time fork that isn't even an edge in the topology:
when LLM and deterministic classifiers disagree, `classify` itself
sets `state.final_status = MANUAL_REVIEW_REQUIRED` and the next
conditional gate routes to terminal.

Three concrete failure modes of the linear stepper, observed in
the deployed system on 2026-05-01:

1. **Halt-point ambiguity.** A FAILED record always renders as
   "blocked at apply_effects" because `STATE_PROGRESS["FAILED"] = 9`.
   For a record that halted at `classify` (LLM/deterministic
   disagreement), the rendered halt step is structurally wrong —
   apply_effects never had a chance to run. The trace_data we
   persist (`api/routes/exceptions.py:217`) carries no
   `halted_at_node` field, so the UI cannot project the truth even
   if we wanted it to.

2. **Branch-taken invisibility.** Two records can land on the same
   visible state ("blocked at shadow_audit") via wholly different
   verdicts (RED vs YELLOW) and policy hits. The current view
   surfaces the verdict only as a one-line caption; the *edge that
   was taken* and *why* are not first-class. For audit, this is
   inadequate — a regulator reconstructing a SOX-relevant decision
   needs the verdict on the edge, not buried in a side caption.

3. **Drift between truth and rendering.** The compiled LangGraph
   knows the topology. The UI maintains a parallel `PIPELINE_NODES`
   array as a hardcoded mirror. Every time the orchestration team
   reorders nodes (as happened in ADR-025 — gateway reads moved
   before shadow), the UI either lags or breaks silently. There is
   no architectural lock that forces them to agree.

A tighter-scoped version of this problem ("persist `halted_at_node`
and patch the FAILED→9 mapping") would resolve symptom 1, but
leaves 2 and 3 untouched. The user's framing on this is correct:
the primitive itself is wrong for the topology.

## Personas (acceptance criteria)

Three operator/admin personas drive the decision. They will sign off
on the visualization being fit-for-purpose:

1. **Operator (control-tower triage role).**
   *Triggered:* a record lands in FAILED, BLOCKED, or
   PENDING_REVIEW / ESCALATED. *Need:* answer
   "where did this halt and why?" in under 5 seconds.
   *Default surface:* the executed-events timeline with halt point
   and reason emphasized. Topology view is secondary; they open it
   only when the timeline isn't enough.

2. **Audit team & ASOE Tools Admin (compliance/oversight role).**
   *Triggered:* every record, including autonomous-resolved (GREEN
   path), HITL outcomes, and BLOCKED. *Need:* reconstruct the
   exact path taken, the verdict on each conditional edge, the
   evidence each node operated on. The DAG view IS their primary
   surface — not a secondary affordance.
   *Acceptance bar:* the rendered DAG must match the compiled
   graph topology byte-for-byte (no UI-side mirror). The taken
   path must be visually distinct from non-taken edges. Each
   conditional edge must label the verdict that drove the route
   (`GREEN | YELLOW | RED`, `breach | ok`, `no recipe | ok`).

3. **AI / LangGraph maintainers.**
   *Triggered:* every orchestration change (node added, edge
   reordered). *Need:* introduce structural changes to the graph
   without coordinating a synchronized UI patch. *Acceptance bar:*
   the topology is exposed by an authoritative endpoint derived
   from `compiled_graph.get_graph()`. The UI consumes that endpoint;
   it owns no mirror of the node list. ADR-025-style reorders ship
   without UI churn.

The three personas overlap on one architectural requirement: **the
view must derive from the compiled graph + the per-record execution
trace, never from a UI-side hardcode.**

## Decision

Replace `WaterfallStepper` with a **hybrid Pipeline visualization**
made of two surfaces, both trace-derived:

### Surface 1 — Executed Events Timeline (default, operator-first)

A vertical list of *only the nodes that ran*, in chronological order,
with halt point and reason emphasized. Skipped nodes are not rendered;
unreachable nodes are not rendered. Each row carries:

- node label + recorded duration
- per-node decision payload (intent + confidence on `classify`,
  verdict + policy hits on `shadow_audit`, recipe name on
  `select_recipe`, gateway count on `resolve_dependencies`,
  `final_status` on `apply_effects` / `build_analysis`)
- terminal row carries the halt reason (`FAILED at classify —
  LLM/deterministic disagreement`) when the run did not reach
  `build_analysis` cleanly.

Scope: ≤ N rows where N = number of nodes that actually executed.
Typical FAILED-at-classify renders 2 rows (ingest, classify); a
GREEN-path resolution renders 11.

### Surface 2 — Compliance DAG view (audit-first, expandable)

A directed acyclic graph rendered with a layered (Sugiyama / dagre)
layout. Topology comes from a new `GET /api/v1/pipeline/topology`
endpoint that introspects `compiled_graph.get_graph()`. The taken
path for the record is highlighted; non-taken edges are shown but
de-emphasized. Each conditional edge carries its verdict label.

Affordance: a "View pipeline graph" disclosure on the timeline
expands the DAG inline (or opens a modal on small viewports). For
audit users, the DAG view can be the default — controlled by a
user preference (`audit_role: dag_default`) that the audit / admin
roles get out of the box.

For autonomous-resolved (GREEN) records, the audit user opens the
DAG and sees: every edge that fired, every verdict, every recipe
output. For BLOCKED, the same view shows the RED verdict on the
`shadow_audit→build_analysis` edge with policy hits annotated.

### Common backbone — both surfaces are trace-derived

Neither surface has a hardcoded node list. Both consume:

1. `GET /api/v1/pipeline/topology` → `PipelineTopology` (nodes +
   edges + conditional verdict labels). Cached with a topology
   hash; the UI revalidates when the hash changes.
2. `GET /api/v1/exceptions/{id}/trace` → existing trace, extended
   with a new `executed_nodes: list[ExecutedNode]` field carrying
   the actual per-node execution record (entered_at, completed_at,
   duration_ms, status, decision payload, verdict-on-exit).

The timeline is `executed_nodes` rendered as a list. The DAG is
`PipelineTopology` rendered as nodes/edges, with `executed_nodes`
overlaying the taken-path highlight + per-edge verdict labels.

## Architecture

### Backend changes (asoe2)

1. **New introspection helper** in `orchestration/graph.py`:
   ```python
   def get_pipeline_topology() -> PipelineTopology:
       """Derive nodes + edges from compiled_graph.get_graph().
       Conditional edges carry the verdict labels declared in
       _add_common_nodes_and_edges. Pure; safe to cache by hash."""
   ```
   The function reads the compiled graph object — no
   reimplementation, no string parsing. ADR-025-style reorders are
   reflected automatically.

2. **New endpoint** `GET /api/v1/pipeline/topology` — **authentication
   required, no additional permission**. The topology is the graph
   shape, not per-tenant data; the same nodes/edges are returned to
   every authenticated user. Per-record sensitive data (executed
   path, decisions, policy hits) lives on
   `/api/v1/exceptions/{id}/trace` and is gated by the existing
   per-record scoping (§11.3 partner-role retailer_id filter). Adding
   `dashboard:read` here would be permission theater that does not
   reduce data-leakage risk and adds a coupling point. Returns a
   `PipelineTopology` with a stable `topology_hash` so the UI can
   cache aggressively.

3. **Trace extension**: every node in `orchestration/nodes.py`
   appends an `ExecutedNode` entry to a new
   `state.execution_trace: list[ExecutedNode]` field. The node
   wraps its real work and emits:
   ```python
   ExecutedNode(
       node="classify",
       entered_at=t0, completed_at=t1, duration_ms=d,
       timestamp=t0,                  # convenience top-level (matches existing trace style)
       status="completed" | "halted" | "errored",
       decision={"intent": ..., "confidence": ...},
       exit_verdict="cross_check_disagreement",  # for conditional gates
       policy_hits=[],                # populated on shadow_audit; [] elsewhere
       sub_spans=[],                  # populated on resolve_dependencies (per-gateway timing)
   )
   ```
   Persisted alongside `intent_confidence` in `trace_data`.

   **Re-entry & reanalysis semantics.** Each LangGraph `invoke()`
   produces exactly one `executed_nodes` list. `build_analysis` is
   reachable from many edges but executes at most once per invoke
   (the AUDIT_CONTEXT_MISSING guard is intra-node early-exit, not a
   second invocation). Reanalysis is captured separately by the
   existing `reanalysis_history` field on the exception record —
   each reanalysis writes a fresh `trace_data` (and therefore a fresh
   `executed_nodes` list) via `store_trace`. The DAG/timeline always
   renders the **current** trace; prior reanalysis states are
   accessible via the existing reanalysis-history surface. This is
   why an explicit `node_execution_id` per `ExecutedNode` is **not**
   required (review-board amendment rejected on these grounds — see
   end of doc).

   **resolve_dependencies sub-spans.** This node fans gateway calls
   out via `concurrent.futures`. The single `ExecutedNode` entry's
   `duration_ms` is the wall-clock span of the fan-out; per-gateway
   timing lives in `sub_spans: list[GatewayCallSpan]` for the
   timeline's nested expand. The DAG view renders one node, not N
   sub-nodes — this matches the orchestration topology.

   **WebSocket batching.** The existing `WSEvent.pipeline_progress`
   factory (currently uncalled per ADR-026 §Phase B.2) is the
   emission point. To avoid ~11× event volume amplification on the
   already-fragile WS path (Container Apps closes idle WS at 4 min;
   see asoe-ui commit `ec69bb5` polling fallback), emit **one event
   per record state change** carrying the full `executed_nodes`
   list to date — not one event per node completion. The UI is
   already idempotent on full-payload refresh via
   `fetchData({silent:true})`, so this fits the existing reconcile
   path.

4. **Schemas** (`api/schemas.py`):
   ```python
   class PipelineTopologyNode(BaseModel):
       id: str          # canonical node name
       label: str       # human-readable
       kind: Literal["node", "terminal"]

   class PipelineTopologyEdge(BaseModel):
       from_node: str
       to_node: str
       conditional: bool
       verdict_label: Optional[str]  # e.g. "GREEN", "breach", "no recipe"

   class PipelineTopology(BaseModel):
       topology_hash: str
       nodes: list[PipelineTopologyNode]
       edges: list[PipelineTopologyEdge]

   class GatewayCallSpan(BaseModel):
       gateway: str                 # canonical gateway name
       started_at: datetime
       finished_at: Optional[datetime]
       duration_ms: Optional[int]
       status: Literal["ok", "error", "timeout"]

   class ExecutedNode(BaseModel):
       node: str
       entered_at: datetime
       completed_at: Optional[datetime]
       duration_ms: Optional[int]
       timestamp: datetime          # convenience: == entered_at, kept for trace-style consistency
       status: Literal["completed", "halted", "errored"]
       decision: dict[str, Any]
       exit_verdict: Optional[str]
       policy_hits: list[str] = []  # populated on shadow_audit; [] elsewhere
       sub_spans: list[GatewayCallSpan] = []  # populated on resolve_dependencies; [] elsewhere
   ```

   **Deprecation of overlapping `TraceResponse` fields.** Once
   `executed_nodes` is the authoritative surface,
   `TraceResponse.shadow_verdict`, `shadow_policy_hits`, and
   `gateway_calls` become redundant projections of per-node data.
   Plan: keep them populated for one release alongside
   `executed_nodes` (back-compat), mark them deprecated in their
   field docstrings, retire them in the release after Phase D
   ships. Indefinite dual-write is exactly the drift surface this
   ADR is trying to retire.

### Frontend changes (asoe-ui)

1. **Delete** `STATE_PROGRESS` and `PIPELINE_NODES` from
   `src/app/exceptions/shared.tsx`. They are the drift surface. The
   UI no longer owns *any* topology knowledge.
2. **Replace** `WaterfallStepper.tsx` with two components:
   - `EventsTimeline.tsx` — renders `executed_nodes` from the
     trace; halt point styling for non-COMPLETE final_status;
     per-node decision surfacing; sub-spans nested-expand for
     `resolve_dependencies`; **streams in via the
     `pipeline_progress` WS event** (full-payload refresh, no
     per-node patching).
   - `PipelineDAG.tsx` — renders `PipelineTopology` using
     **`dagre` + a thin custom SVG renderer**. **Hard ceiling:
     20 KB gzipped on the lazy-loaded chunk.** Override requires
     written justification on bundle impact. `@reactflow/core` is
     ruled out as default — it ships drag/zoom/mini-map
     primitives this view does not need (every interaction is
     read-only "show me the path the record took"). Highlighted-
     path overlay driven by `executed_nodes`; verdict labels on
     conditional edges.
3. **`DiagnosticsSection.tsx`** composes the two: timeline by
   default, DAG behind a disclosure for operators, DAG-default for
   audit roles (driven by a `useUserRole()` preference, not by
   role-string sniffing in JSX — keeps Guardrail #2 intact).
4. **Architectural lock test** (`tests/architectural/`): assert that
   no `.tsx` file under `src/app/exceptions/` references hardcoded
   node names from a closed enum. The compiled graph is the source.

### Implementation phases

| Phase | Scope | Effort | Gate |
|---|---|---|---|
| **A.0** | **Verdict-vocabulary registration.** Declare per-gate verdict labels alongside each `add_conditional_edges` call in `orchestration/graph.py` (e.g. via a sibling `_VERDICT_LABELS["shadow_audit"] = {...}` registry, or by migrating `route_after_gate` to return the verdict string). The compiled graph alone exposes only `{terminal, continue}` keys; the human verdicts (`GREEN | breach | no recipe`) live in the route function's implicit contract today. Phase A's introspection helper consumes this registration; without it, "verdict on edge" in the DAG is unrooted. **Load-bearing for Phase D's audit-fitness claim.** | ~1 day | AI/LangGraph review (verdict vocabulary covers every conditional gate, including the classify-time disagreement which is rendered as an implicit gate) |
| **A** | Backend topology endpoint + introspection helper consuming the A.0 registration. Schema + endpoint, no UI consumer yet. Architectural lock test asserts compiled-graph parity. | ~1 day | AI/LangGraph review (does the introspection capture all conditional edges + verdict labels correctly?) |
| **B** | Trace extension: `ExecutedNode` appended by every orchestration node, with `policy_hits`, `timestamp`, and `sub_spans` populated. Persisted into `trace_data`. Tests assert per-node entries land in the right order with the right exit verdicts on disagreement / RED / YELLOW / breach / no-recipe. **`pipeline_progress` WS event starts firing — batched to one event per record state change with the full `executed_nodes` list.** Real-time *timeline append* falls out of this for free; real-time *DAG re-render* stays out of scope (Phase F). | ~2 days | Compliance review (do `executed_nodes` entries carry every verdict, policy hit, and timing field an auditor needs to reconstruct a SOX-relevant decision?) |
| **C** | `EventsTimeline.tsx` replacing the default WaterfallStepper render, consuming the streamed payload from Phase B. Hardcoded `PIPELINE_NODES` and `STATE_PROGRESS` deleted. Architectural lock test added. | ~1 day | Operator feedback on a synthetic trace (does the halt-point read clearly in <5s?) |
| **D** | `PipelineDAG.tsx` behind disclosure. `dagre` + custom SVG, taken-path highlighting, verdict-on-edge labels. Bundle ceiling enforced via CI check on the lazy chunk size. | ~1.5 days | Audit team sign-off on **four** sample traces: autonomous-resolved (GREEN), HITL (YELLOW shadow), BLOCKED (RED shadow), and **`FAILED at classify` (cross-check disagreement)** — the original SMK-CB-001 case |
| **E** | Role-based default surface (audit users → DAG default). | ~0.5 day | RBAC review |

Total: **~7-8 days** end to end. A.0 is load-bearing — it must
land first. A and B unblock symptom 1 (and the `FAILED at classify`
canonical trace) without touching the UI; Compliance can sign off
on the audit story before Frontend starts work, which is the right
risk-staging given the SOX surface.

## Consequences

### Good

- **Single source of truth** for the topology. ADR-025-class
  reorders ripple to the UI through a topology hash bump, not a
  coordinated patch.
- **Halt point is honest.** A FAILED-at-classify record renders as
  halted at classify, with the disagreement verdict on the
  classify→load_skill edge.
- **Audit story is complete.** Every conditional verdict is
  rendered on the edge it drove. No verdict survives only as a
  side caption.
- **Operator load drops.** Default timeline is N executed rows,
  not 11 mostly-skipped rows.
- **Architectural lock.** Hardcoded enum values for node names
  cannot survive in `.tsx` (Guardrail #2 already prohibits, this
  extends the prohibition to pipeline nodes).

### Bad / costs

- New endpoint + new schema + new UI components. Real surface area.
- Adds a `dagre` dependency to the UI bundle (≈12 KB gzipped, custom
  SVG renderer kept under the 20 KB lazy-chunk ceiling). Audit-role
  users see it on detail-page open; non-audit users lazy-load only
  when they expand the DAG affordance.
- The DAG view introduces visual primitives (curved edges, layered
  layout) that the existing design system doesn't yet have token
  coverage for. SKILL.md will need a small extension for graph-
  specific tokens (edge stroke, taken-path highlight, verdict
  badge on edge).
- Existing tests that assert `PIPELINE_NODES` ordering need
  retirement.

### Neutral

- Per-node duration tracking lands as a side effect (Phase B emits
  `entered_at` / `completed_at`). This was deferred in ADR-026 §Phase
  B.2; no new work, just gets done here.

## Alternatives considered

1. **Keep WaterfallStepper, persist `halted_at_node`, patch
   `STATE_PROGRESS`.** Resolves symptom 1 only. Symptom 2 (branch
   invisibility) and symptom 3 (drift) survive. Cheap (~half a
   day) but doesn't satisfy audit-team acceptance criteria.
   Rejected by the personas analysis.
2. **DAG-only view (no timeline).** Honest for audit, too dense
   for operator triage. The operator needs "where did it halt"
   answerable in one glance — a 11-node DAG is not that.
3. **Activity stream only (event-log style, like a syslog).**
   Reads like a log, loses topology entirely. Audit team signaled
   this fails their evidence-of-execution test: "I can't tell
   from a log alone whether the orchestrator skipped a node by
   policy or by bug."
4. **Embed LangGraph's built-in graph visualizer.** Generates a
   Mermaid / Graphviz string. Wrong primitive — read-only, not
   record-aware, can't overlay an executed path.
5. **Per-execution `node_execution_id` UUID on `ExecutedNode`** (review
   board recommendation, **rejected**). Proposed to disambiguate
   `build_analysis` re-entry within a single trace and to
   future-proof retry visualization. Rejected because: (a) within
   a single LangGraph `invoke()`, `build_analysis` runs at most
   once — the AUDIT_CONTEXT_MISSING guard is intra-node early-exit,
   not a second invocation; (b) reanalysis is captured as a
   separate `reanalysis_history` entry with its own fresh
   `trace_data` (and therefore its own `executed_nodes` list),
   not as multiple executions on a single trace; (c) retry
   visualization is explicitly out-of-scope (see "Out of scope"
   below) and would belong on its own ADR with its own data
   model. Adding a UUID per node now would be carrying weight for
   a use case we have committed not to render.
6. **`@reactflow/core` over `dagre` + custom SVG.** ~45 KB
   gzipped, ships drag/zoom/mini-map primitives this view does
   not need. Read-only DAGs can be rendered with dagre (layout)
   + a thin SVG component (≈12 KB total). Override allowed only
   with written justification on bundle impact.
7. **`dashboard:read` permission on the topology endpoint** (review
   board suggestion, **rejected**). The topology is the graph
   shape — the same nodes/edges for every authenticated user — and
   carries no per-tenant data. Per-record sensitive data lives on
   the existing trace endpoint, which already enforces partner-
   role retailer_id scoping. Adding a permission that does not
   reduce data-leakage risk would be permission theater.

## Reviewers required

This ADR doesn't ship until the following sign off, in this order:

1. **AI / LangGraph maintainers** — Phase A's introspection helper
   correctly captures the topology, including conditional edge
   labels. They own the source of truth.
2. **Compliance / Audit Team** — Phase B's `ExecutedNode` shape
   carries every verdict, policy hit, and timing field they need
   for SOX-relevant evidence reconstruction. They specify the
   acceptance bar.
3. **ASOE Tools Admin** — DAG view as default surface for the
   admin role; preference plumbing meets their workflow.
4. **Frontend Platform** — `PipelineDAG.tsx` library choice
   (`dagre` + custom SVG vs `@reactflow/core` vs other), bundle
   size, design-token extensions for graph primitives.
5. **Compliance veto holder (Verdict 2026-04-22 / Guardrail #6)** —
   confirms the hybrid view does not introduce partial-truth
   states (e.g. an edge labelled "GREEN" without the policy hits
   that informed it).

## Out of scope (do not conflate)

- **Per-node retry visualization.** Recipes and gateways have
  retry semantics; rendering attempts on the DAG is a separate
  surface. Not in this ADR.
- **Reanalysis as multiple executions on one trace.** Reanalysis
  is captured as a separate `reanalysis_history` entry — each
  reanalysis writes a fresh `trace_data` and therefore a fresh
  `executed_nodes` list. The pipeline view always renders the
  current trace; prior states are reachable through the
  reanalysis-history surface. No need to render multiple
  `build_analysis` executions on a single DAG.
- **Real-time DAG re-render during execution.** The WebSocket
  `pipeline_progress` event (Phase B emits it) is the natural
  mechanism; real-time *timeline append* falls out for free
  because the timeline reconciles on full-payload refresh.
  Real-time DAG re-render adds layout-flicker and back-pressure
  concerns that don't apply to the timeline. Phase F (post-D).
- **Cross-record DAG aggregation** (e.g., "show me all records
  that took the YELLOW shadow path this week"). That's a
  reporting surface, not an exception-detail surface. Belongs in
  a future Insights / Analytics ADR.
- **Embedding the DAG in the audit hash chain.** The execution
  trace already feeds the disposition hash chain (ADR-023); the
  DAG is a render of that trace, not new evidence. No chain
  changes.

---

## Open questions for the review meeting

1. **Topology endpoint caching.** Does the topology hash change
   often enough to warrant client-side caching, or do we always
   fetch on detail-page load? Vote: cache by hash, validate on
   `useHealth` polling tick. *Status: still open, low-risk; pick
   at Phase A implementation.*
2. ~~**DAG library.**~~ **RESOLVED (rev. 2):** `dagre` + custom
   SVG, hard ceiling 20 KB gzipped on the lazy chunk.
   `@reactflow/core` ruled out as default.
3. ~~**Verdict-label vocabulary on conditional edges.**~~
   **RESOLVED (rev. 2):** Phase A.0 declares per-gate verdict
   labels alongside each `add_conditional_edges` call (sibling
   registration or migrate `route_after_gate` to return the
   verdict string). Compiled-graph introspection alone is
   insufficient — the compiled graph only exposes
   `{terminal, continue}`, not human verdicts. AI/LangGraph
   owns the registration shape.
4. ~~**Audit-default preference plumbing.**~~ **RESOLVED (rev. 2):**
   per-user setting (`audit_dag_default: bool`) defaulted to true
   for `role ∈ {audit, admin}`, false otherwise. Auditors can
   override the default via a settings toggle. Plumbing in Phase
   E.
5. **Migration of existing records.** Records created before
   Phase B's trace extension have no `executed_nodes`. Timeline
   degrades gracefully (renders the small set of fields already
   in trace_data). DAG view shows topology with no taken-path
   highlight, plus an explicit banner: "execution trace
   pre-dates per-node tracing — full path not available". Better
   than fabricating one. *Status: confirm Compliance accepts
   this banner as evidence of pre-Phase-B records during the
   transition window.*
6. **Phase A.0 registration shape.** Two candidate forms — (a)
   `_VERDICT_LABELS["shadow_audit"] = {"terminal_red": ...,
   "terminal_yellow": ..., "continue_green": "execute_recipe"}`
   alongside `add_conditional_edges`; (b) migrate
   `route_after_gate` to return the verdict string and add a
   second registration mapping verdict → next node. (a) is
   smaller; (b) is more refactorable. AI/LangGraph owns this
   call.
7. **Classify-time disagreement render.** `classify` sets
   `final_status` itself rather than via a graph edge. The DAG
   shows this as an implicit gate: when an `ExecutedNode` for
   `classify` carries `exit_verdict="cross_check_disagreement"`
   AND the next executed node is `build_analysis` (or any
   terminal), the DAG renderer draws an implicit highlighted
   edge from classify → build_analysis labelled with the
   disagreement verdict. This is a render convention; document
   it in Phase D's spec so it's explicit, not folklore.

---

## Amendments after review board (2026-05-01, rev. 2)

The review board's deep technical audit on `core_ui_integration`
flagged eight concerns. Seven are accepted in the body of the ADR
above; one is rejected. Summary delta:

### Accepted (incorporated above)

| # | Amendment | Where applied |
|---|---|---|
| A1 | **Phase A.0 — Verdict-vocabulary registration** as a load-bearing prerequisite for Phase A. Compiled-graph introspection alone exposes only `{terminal, continue}`; human verdicts (`GREEN | breach | no recipe`) need explicit declaration. | Phases table; Open Questions §3, §6 |
| A2 | **`ExecutedNode` extensions:** `policy_hits`, `timestamp`, `sub_spans`. Schema additive, back-compat preserved. | Architecture §3, §4 |
| A3 | **Fourth canonical trace** for Phase D sign-off: `FAILED at classify` (cross-check disagreement). The original SMK-CB-001 trigger; without it the canonical-trace set doesn't prove the symptom-1 fix. | Phases table (Phase D Gate); Open Questions §7 |
| A4 | **`dagre` + custom SVG pinned**, hard ceiling 20 KB gzipped on the lazy chunk, override requires written bundle-impact justification. | Frontend changes §2; Alternatives §6; Open Questions §2 |
| A5 | **WS batching:** one `pipeline_progress` event per record state change carrying full `executed_nodes`-to-date, not one event per node. Reuses the UI's existing idempotent reconcile path. | Architecture §3 (WebSocket batching subsection) |
| A6 | **Drop `dashboard:read`** on `/api/v1/pipeline/topology` — authentication only. Topology has no per-tenant data; per-record sensitive data is already gated on the trace endpoint. | Architecture §2; Alternatives §7 |
| A7 | **Real-time timeline append moves into Phase B** (free with `pipeline_progress` emission); real-time **DAG re-render** stays in Phase F. | Phases table (Phase B); Out of scope |
| A8 | **Effort revised to 7-8 days** (was 5.5) to accommodate Phase A.0 + extended Phase B. | Phases table (Total) |
| A9 | **Sub-spans for `resolve_dependencies`** (`GatewayCallSpan` list) — fan-out gateway timing surfaces in the timeline as nested expand without breaking the one-`ExecutedNode`-per-orchestration-node DAG model. | Architecture §3, §4 |
| A10 | **Deprecation plan** for overlapping `TraceResponse` fields (`shadow_verdict`, `shadow_policy_hits`, `gateway_calls`): keep one release for back-compat, mark deprecated, retire after Phase D. | Architecture §4 (Deprecation subsection) |

### Rejected

| # | Amendment | Rationale |
|---|---|---|
| R1 | **`node_execution_id` UUID per `ExecutedNode`** for `build_analysis` re-entry and retry future-proofing. | Reanalysis is captured as a separate `reanalysis_history` entry — each reanalysis writes a fresh `trace_data` and therefore a fresh `executed_nodes` list. Within a single LangGraph `invoke()`, `build_analysis` runs at most once (the AUDIT_CONTEXT_MISSING guard is intra-node early-exit, not a second invocation). Retry visualization is explicitly out-of-scope. Adding a UUID per node now would carry weight for a use case we have committed not to render. See Alternatives §5 for the full argument. |
