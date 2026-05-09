from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict, model_validator


class Intent(str, Enum):
    CONTRACTUAL_CORRECTION = "CONTRACTUAL_CORRECTION"
    CREDIT_BLOCK = "CREDIT_BLOCK"
    MASS_PRICING_ERROR = "MASS_PRICING_ERROR"
    DUPLICATE_PO = "DUPLICATE_PO"
    PRICE_HOLD_RELEASE = "PRICE_HOLD_RELEASE"
    EDI_MISMATCH = "EDI_MISMATCH"
    BACK_ORDER = "BACK_ORDER"
    OVER_MAX = "OVER_MAX"
    MIN_ORDER_QTY = "MIN_ORDER_QTY"
    PALLET_CONFIG = "PALLET_CONFIG"
    DELIVERY_DELAY = "DELIVERY_DELAY"
    MANUAL_ORDER_INTAKE = "MANUAL_ORDER_INTAKE"
    UNKNOWN = "UNKNOWN"


class ShadowStatus(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class TerminalStatus(str, Enum):
    COMPLETE = "COMPLETE"
    FAIL_TO_HUMAN = "FAIL_TO_HUMAN"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    COMPLETE_WITH_CHILDREN = "COMPLETE_WITH_CHILDREN"
    # Registry-enforced audit gap — the `build_analysis` composition node
    # (api/analysis_composer.py) emits this when one or more audit-bearing
    # fields declared in `compliance/audit_bearing_registry.yaml` cannot
    # be populated from recipe output / enrichment context / event. Named
    # distinctly from FAIL_TO_HUMAN so auditors see "compliance data was
    # missing" rather than "the pipeline crashed". Routes to FAILED in
    # the lifecycle (no reviewer path — the record cannot be audited).
    AUDIT_CONTEXT_MISSING = "AUDIT_CONTEXT_MISSING"


# Single source of truth: maps TerminalStatus to exception lifecycle state.
# Consumed by api/store.py (in-memory) and db/repository.py (database).
STATUS_TO_LIFECYCLE: Dict[str, str] = {
    "COMPLETE": "RESOLVED",
    "FAIL_TO_HUMAN": "FAILED",
    "MANUAL_REVIEW_REQUIRED": "PENDING_REVIEW",
    "BLOCKED": "BLOCKED",
    "REJECTED": "REJECTED",
    "COMPLETE_WITH_CHILDREN": "RESOLVED",
    "AUDIT_CONTEXT_MISSING": "FAILED",
}

# 12-state exception lifecycle (architecture_v3.md §9.1).
# Consumed by api/routes/health.py and stats queries.
# PENDING_ADMIN_REVIEW: RED-verdict admin release (three-tier HITL model).
LIFECYCLE_STATES: List[str] = [
    "INGESTED", "CLASSIFYING", "AUDITING", "PENDING_REVIEW", "ESCALATED",
    "PENDING_ADMIN_REVIEW", "PENDING_COSIGN", "RESOLVED",
    "FAILED", "BLOCKED", "REJECTED", "CLOSED",
]

# Valid source states for each HITL action.
# Consumed by api/routes/exceptions.py for state-machine enforcement.
#
# Option A (Phase 1 unified action model): privileged users (manager+) can
# Override the agent's recommendation on GREEN (RESOLVED) and RED (BLOCKED)
# lifecycles, not only YELLOW (PENDING_REVIEW/ESCALATED). Escalate is a
# separate routing event with its own endpoint and its own source states.
HITL_OVERRIDE_STATES = {
    "PENDING_REVIEW", "ESCALATED", "RESOLVED", "BLOCKED",
    # Admin-released RED exceptions land here and must be dispositionable
    # by the releasing admin or a manager. Without this the admin-release
    # → disposition flow returns 409.
    "PENDING_ADMIN_REVIEW",
}
# Four-eyes (Phase 2 #5): a /override call whose financial_impact_usd >=
# HIGH_VALUE_OVERRIDE_THRESHOLD_USD transitions the record to PENDING_COSIGN
# instead of RESOLVED. A second manager+ must cosign via /override/cosign
# before the action is applied. The cosign caller must not be the initiator
# (enforced server-side alongside the standard SoD check).
COSIGN_ELIGIBLE_STATES = {"PENDING_COSIGN"}
HITL_DISPOSITION_STATES = {"PENDING_REVIEW", "ESCALATED", "PENDING_ADMIN_REVIEW"}
"""Source states for /disposition sub-type APPROVE and REJECT. Phase 3
collapsed HITL_APPROVE_STATES + HITL_REJECT_STATES into this single set —
they had identical membership and the endpoints they gated no longer
exist."""
CHALLENGE_SOURCE_STATES = {"RESOLVED"}
ADMIN_RELEASE_SOURCE_STATES = {"BLOCKED"}
# Escalate is a pure routing event decoupled from Override.
# Already-ESCALATED and RESOLVED are 409 (the former is a no-op; the latter
# already has a dedicated Challenge path).
ESCALATE_ELIGIBLE_STATES = {"PENDING_REVIEW", "FAILED", "BLOCKED"}


class OrderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    line_item: int = 1
    sku: Optional[str] = None
    event_type: str = "EDI_850_PRICE_MISMATCH"
    po_price: float
    sap_base_price: float
    retailer_id: Optional[str] = None
    event_ts: Optional[str] = None
    requester_role: Optional[str] = None
    credit_limit: Optional[float] = None
    current_exposure: Optional[float] = None
    line_count: int = 1
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# OrderCase — ADR-038 Phase H.2 parent entity
# ---------------------------------------------------------------------------
#
# An OrderCase is the parent record for all events / actions on a single
# business order. Created lazily for Automated Orders (on first non-clean
# event) and eagerly for Manual Orders (on email arrival). Holds the SLA
# clock, the case-source classification, and correlation keys that let
# incoming events resolve to an existing case via lookup-or-create.

CaseSource = Literal["manual_order", "automated_order"]
"""ADR-038 §3.1 — Manual = email/phone/fax (CSR reads prose to extract);
Automated = EDI X12 / portal / API feed / FTP / VMI (no prose to extract)."""

CaseStatus = Literal[
    "OPEN_AGENT_PROCESSING",  # agent is currently working
    "OPEN_AWAITING_HUMAN",    # MANUAL_REVIEW_REQUIRED on a child
    "OPEN_AWAITING_BUYER",    # REQUEST_CLARIFICATION sent
    "OPEN_AWAITING_ERP",      # submitted; waiting on ERP confirmation
    "RESOLVED",               # all children terminal-closed
    "FAILED",                 # case-level FAIL_TO_HUMAN
    "BLOCKED",                # case-level RED verdict
]
"""ADR-038 §6.1 — case lifecycle. Distinct from `LIFECYCLE_STATES`
which describes the per-exception lifecycle. The case-level lifecycle
tracks "what's blocking forward progress on this business order"."""

CaseTier = Literal[1, 2, 3]
"""ADR-038 §7.1 — graduated materialisation policy. T1 = stateless,
T2 = stateful, T3 = stateful + compacted. Forward-only transitions
(never demote)."""


class OrderCase(BaseModel):
    """ADR-038 Phase H.2 parent entity.

    Created via `case_store.lookup_or_create()` keyed on whichever
    correlation identifiers the inbound event carries. SLA clock starts
    at `opened_at`; tier is set per the materialisation policy in
    ADR-038 §7.1. `bundle_version_at_open` stamps the L0 skill bundle
    version active when the case opened so audit can replay against
    the historical knowledge surface.

    `working_memory_summary` is populated by the Phase H.7 compaction
    protocol; it's None until first compaction fires.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    customer_id: Optional[str] = None

    source: CaseSource
    source_channel: str
    """Finer-grained channel within `source`. ADR-038 §3.3 allowed
    values: 'email' | 'phone' | 'fax' for manual_order; 'edi_x12_850'
    | 'edi_x12_855' | 'portal' | 'api_feed' | 'ftp_csv' | 'ftp_xml'
    | 'vmi_replenishment' for automated_order. The string is open
    so new channels don't require a contract bump; routing decisions
    don't branch on it (recipes handle issues, not channels)."""

    customer_po_number: Optional[str] = None
    sales_order_id: Optional[str] = None
    edi_transaction_id: Optional[str] = None
    source_email_id: Optional[str] = None

    opened_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    closed_at: Optional[str] = None
    status: CaseStatus = "OPEN_AGENT_PROCESSING"
    sla_deadline: Optional[str] = None

    tier: CaseTier = 2
    """Default T2 (stateful). Tier-1 stateless events do not
    materialise an OrderCase at all per ADR-038 §7.1; this field is
    only present on records that exist as cases."""

    working_memory_summary: Optional[str] = None
    last_compaction_at: Optional[str] = None
    bundle_version_at_open: Optional[str] = None


CaseCorrelationKeyType = Literal[
    "customer_po_number",
    "sales_order_id",
    "edi_transaction_id",
    "source_email_id",
]
"""ADR-038 §6.2 — the four key types that resolve an event to a case.
Resolution priority is sales_order_id → customer_po → edi_transaction_id
→ source_email_id; first match wins."""


class CaseCorrelationKey(BaseModel):
    """One row in the `case_correlation_keys` table (V010).

    ADR-038 §6.2 — `(tenant_id, key_type, key_value)` is the primary
    key; multiple keys can resolve to the same `case_id` (e.g., the
    same case has both a customer PO number and an SO id and a
    source email). The first event to arrive registers whatever
    keys it carries; subsequent events enrich the set as new
    identifiers appear (the ERP creates the SO and that key joins
    the existing case).
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    key_type: CaseCorrelationKeyType
    key_value: str
    case_id: str
    registered_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class PricingDiscrepancy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delta: float
    delta_pct: float
    within_threshold: bool


class RagContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunks: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SkillDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    text: str
    recipes: List[str] = Field(default_factory=list)


class RecipeInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_name: str
    params: Dict[str, Any]


class ShadowLLMVerdict(BaseModel):
    """ADR-039 L2 LLM Shadow second-opinion verdict.

    Constrained-output result of one ``compliance/shadow_llm.py``
    inference. Recorded on every L2-invoked event regardless of
    rollout phase (X.1 observe-only ships this populated; only
    later phases let it actually move the final verdict per
    ADR-039 §6).

    The shape is byte-stable so audit queries
    (``llm_shadow_verdict.action = 'DISAGREE_DOWNGRADE'``) work
    against the persisted record. Note the deliberate absence of
    any ``DISAGREE_UPGRADE`` enum: the asymmetric authority rule
    in ADR-039 §4 is enforced **in the schema**, not bolted on
    after the fact.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"]
    """ADR-039 §3.2 — the closed action vocabulary. No
    ``DISAGREE_UPGRADE``; structurally impossible."""

    reason: str = Field(min_length=1, max_length=200)
    """One-sentence rationale (≤200 chars). Surfaced verbatim to
    the human reviewer when L2 downgrades a verdict (ADR-039
    §4.5). Never free-form chain-of-thought."""

    confidence: float = Field(ge=0.0, le=1.0)
    """LLM-reported confidence in the action."""

    policy_concerns: List[str] = Field(default_factory=list)
    """Named concerns from the closed
    ``knowledge/shadow_llm/concerns_vocabulary.yaml`` list. Free-
    form strings are validated out at gateway-time; this list is
    typed as ``List[str]`` so the schema works pre-vocabulary-load."""

    bundle_version: str = ""
    """L0 ``knowledge/shadow_llm`` bundle version active when the
    L2 call was made. Audit replay requires (model_id,
    bundle_version, temperature, prompt) byte-identity."""

    model_id: str = ""
    """Resolved model id reported by the provider (e.g.
    ``claude-haiku-4-5-20251001``). Audit-bearing per
    LLMProvenance."""

    request_id: Optional[str] = None
    """Provider request id for support tickets (Anthropic
    ``request-id`` header / OpenAI ``x-request-id`` etc.)."""

    cache_hit: bool = False
    """True when the verdict was served from the L4 per-tenant
    shadow-LLM cache (ADR-039 §5.5). Even cache hits are recorded
    so SLI tracking sees the cache yield."""

    latency_ms: int = 0
    """Wall-clock latency for this invocation. 0 on cache hit."""

    cost_usd_estimate: float = 0.0
    """USD spend estimate for this invocation. 0 on cache hit."""


class ComplianceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ShadowStatus
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    reasons: List[str] = Field(default_factory=list)
    policy_hits: List[str] = Field(default_factory=list)
    constrained_by: Optional[str] = None
    llm_shadow_verdict: Optional[ShadowLLMVerdict] = None
    """ADR-039 X.1 — populated when the L2 LLM Shadow was invoked
    (gating per ADR-039 §5.2 fired). ``None`` when L2 was not
    invoked (gating not triggered, L1 returned RED, or L2 was
    unavailable / timed out — fall-through events are recorded
    elsewhere as SLI counters, not on this field)."""


class ExecutionLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: str
    recipe_name: Optional[str] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)
    constrained_outputs: Dict[str, str] = Field(default_factory=dict)
    intent_selected: Optional[str] = None
    rag_chunks: List[str] = Field(default_factory=list)
    shadow_policy_hits: List[str] = Field(default_factory=list)
    skill_name: Optional[str] = None
    shadow_verdict: Optional[str] = None
    # Override audit fields (Phase E) — populated when a human overrides
    # the agent's recommended action.  None when auto-executed.
    resolved_by: Optional[str] = None
    resolved_action: Optional[str] = None
    resolution_notes: Optional[str] = None


class ShadowEnforcement(BaseModel):
    """Typed result of ComplianceShadow.enforce().

    Captures the routing decision derived from a ComplianceDecision verdict:
      GREEN  → action="PROCEED"   — auto-proceed is allowed
      YELLOW → action="ESCALATE"  — route to MANUAL_REVIEW_REQUIRED
      RED    → action="BLOCK"     — halt immediately; explain breach

    This model is the Phase 2 enforcement contract.  Recipe execution must
    never appear here — enforcement is routing only.
    """

    model_config = ConfigDict(extra="forbid")
    action: Literal["PROCEED", "BLOCK", "ESCALATE"]
    trace_id: str
    explanation: str


class CircuitBreakerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: bool
    reasons: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Gateway contracts — Hexagonal Architecture (Ports & Adapters)
# ---------------------------------------------------------------------------


class GatewayRequest(BaseModel):
    """Typed request to an infrastructure gateway."""

    model_config = ConfigDict(extra="forbid")
    gateway_name: str
    operation: str
    params: Dict[str, Any]
    trace_id: str
    timeout_ms: int = 5000


class GatewayResponse(BaseModel):
    """Typed response from an infrastructure gateway."""

    model_config = ConfigDict(extra="forbid")
    gateway_name: str
    operation: str
    status: Literal["SUCCESS", "FAILED", "TIMEOUT", "UNAVAILABLE"]
    data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class GatewayDependency(BaseModel):
    """Declares data a recipe needs from a gateway before execution.

    The orchestration layer resolves dependencies by calling the named
    gateway operation and storing the result under ``result_key`` in
    ``GraphState.enrichment_context``. Recipes never call gateways
    directly.

    ``required_for_audit`` controls failure semantics in
    ``resolve_dependencies``:
      * True  (default) — gateway failure halts the graph with
                          FAIL_TO_HUMAN. Use for evidence the operator
                          MUST see (audit-bearing fields).
      * False           — gateway failure logs and writes an empty
                          dict to enrichment_context[result_key];
                          the composer then routes to
                          AUDIT_CONTEXT_MISSING via the standard
                          coverage check rather than crashing the run.
                          Use for evidence that's nice-to-have but not
                          required to present the record for review.
    """

    model_config = ConfigDict(extra="forbid")
    gateway_name: str
    operation: str
    params_from_state: Dict[str, str] = Field(
        default_factory=dict,
        description="Maps gateway param name → dot-path into GraphState (e.g. 'event.order_id')",
    )
    result_key: str
    required_for_audit: bool = True


class GatewayEffect(BaseModel):
    """Declares a side effect a recipe triggers after execution.

    The orchestration layer applies effects by calling the named gateway
    operation with params extracted from recipe output fields.
    """

    model_config = ConfigDict(extra="forbid")
    gateway_name: str
    operation: str
    params_from_output: Dict[str, str] = Field(
        default_factory=dict,
        description="Maps gateway param name → recipe output field name",
    )


# ---------------------------------------------------------------------------
# Workflow contracts — Saga pattern (Garcia-Molina)
# ---------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """A single step in a multi-intent workflow."""

    model_config = ConfigDict(extra="forbid")
    step_id: str
    intent: Intent
    description: str
    input_mapping: Dict[str, str] = Field(
        default_factory=dict,
        description="Maps metadata key → previous step output field (carries state forward)",
    )
    compensation_recipe: Optional[str] = None


class WorkflowDefinition(BaseModel):
    """Ordered sequence of steps forming a multi-intent workflow."""

    model_config = ConfigDict(extra="forbid")
    workflow_id: str
    name: str
    steps: List[WorkflowStep]


class WorkflowStepResult(BaseModel):
    """Result of a single workflow step execution."""

    model_config = ConfigDict(extra="forbid")
    step_id: str
    intent: Intent
    final_status: TerminalStatus
    execution_log: Optional[ExecutionLog] = None
    shadow_verdict: Optional[str] = None
    explanation: Optional[str] = None


class WorkflowResult(BaseModel):
    """Aggregate result of a multi-step workflow."""

    model_config = ConfigDict(extra="forbid")
    workflow_id: str
    workflow_name: str
    status: Literal["COMPLETE", "FAILED", "COMPENSATED", "PARTIAL"]
    step_results: List[WorkflowStepResult]
    compensation_log: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph state — single source of truth for one execution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LLM provenance — one entry per trio call when a remote LLM backend served
# the call. Recorded by RemoteLLMBackend after each provider invocation
# (success OR fallback), pulled by orchestration nodes onto GraphState, and
# surfaced via TraceRecord + LangFuse generation spans.
# ---------------------------------------------------------------------------


class LLMCallTrace(BaseModel):
    """SOX-grade per-call telemetry for one LLM trio invocation.

    Audit fields (provider, model_id, request_id) carry the
    LLMProvenance section in compliance/audit_bearing_registry.yaml.
    Token / cost fields drive cost guardrails and dashboards. Hash
    fields support cross-pod cache-hit verification without leaking
    prompt content into logs.
    """

    model_config = ConfigDict(extra="forbid")

    task: Literal["intent", "recipe", "shadow", "shadow_llm"]
    """Which trio method this call served. ``shadow_llm`` is the
    ADR-039 L2 second-opinion call — distinct from ``shadow`` so
    audit queries can separate L1 deterministic from L2 LLM telemetry."""

    provider: str
    """LLMProvider value used for this call ('anthropic' / 'openai' /
    'google' / 'ollama' / 'huggingface' / 'fallback' / etc.). Keep
    in sync with contracts/policy.py LLMProvider enum."""

    model_id: str
    """Resolved model id reported BY the provider response (may
    differ from the configured alias when the provider rewrites,
    e.g. Anthropic adds a date suffix). Audit-bearing per
    LLMProvenance."""

    request_id: Optional[str] = None
    """Provider request id — Anthropic 'request-id' header, OpenAI
    'x-request-id' header, HF chat completion id. Critical for
    support tickets. Audit-bearing per LLMProvenance."""

    prompt_hash: str = ""
    """SHA-256 of the cacheable system + tools bytes the provider
    received. Two calls with the same prompt_hash should hit prompt
    cache (when supported). Used by tests/test_llm_cache_invalidators
    style assertions; never logged at full length."""

    tool_call_hash: str = ""
    """SHA-256 of (tool_name + JSON-canonical arguments). Lets the
    audit trail detect identical tool calls across runs without
    storing PII-shaped argument payloads."""

    input_tokens: int = 0
    """Prompt tokens NOT served from cache (charged at full input
    rate). For OpenAI this excludes prompt_tokens_details.cached_tokens.
    For Anthropic this excludes cache_read_input_tokens."""

    output_tokens: int = 0
    """Completion / output tokens."""

    cache_read_input_tokens: int = 0
    """Tokens served from prompt cache (charged at ~0.1× input rate).
    Anthropic ephemeral / OpenAI automatic / HF + Ollama: 0
    (no client-visible caching)."""

    cache_creation_input_tokens: int = 0
    """Tokens written to prompt cache (charged at ~1.25× input
    rate). Anthropic 5-minute TTL only in V1."""

    latency_ms: int = 0
    """Wall-clock latency observed by the backend (provider call
    only — does not include sanitiser / Pydantic validate)."""

    cost_usd_estimate: float = 0.0
    """USD spend estimate from contracts/policy.py
    LLM_PRICING_USD_PER_M_TOKENS. 0.0 for provider/model
    combinations not in the pricing table (logged as a warning
    once at startup)."""

    stop_reason: Optional[str] = None
    """Provider-native stop reason ('end_turn' / 'tool_use' /
    'stop' / 'length'). Telemetry only — control flow never
    branches on it."""

    skill_md_version: str = ""
    """SHA-256 of the verbatim SKILL.md catalog at call time. A
    catalog edit between runs produces a new value, which lets the
    audit trail tie a specific decision to a specific reasoning
    surface."""

    fallback_to_deterministic: bool = False
    """True when the remote provider failed (ProviderError,
    CircuitOpen, budget hard-block, validation error, etc.) and
    the deterministic fallback served the call instead. Surfaces
    silent degradation to dashboards."""

    fallback_reason: Optional[str] = None
    """Short token classifying the fallback cause: 'rate_limit',
    'timeout', 'connection', 'auth', 'schema_mismatch',
    'server_error', 'circuit_open', 'budget_hard_block',
    'validation_error', 'unknown'. None when the call succeeded."""

    cross_check_disagreement: Optional[bool] = None
    """Only set on intent calls. True when the LLM's intent
    disagreed with the deterministic classifier and the graph was
    routed to MANUAL_REVIEW_REQUIRED. None for recipe/shadow."""

    cross_check_llm_intent: Optional[str] = None
    cross_check_deterministic_intent: Optional[str] = None
    """The two intents recorded at the point of disagreement.
    Telemetry only — the orchestration explanation field carries
    the human-readable form."""


class GraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: OrderEvent
    # Request-scoped tenant id stamped at the API boundary (the
    # orchestration layer has no direct request scope; the API
    # plumbs the value forward). Optional so existing test fixtures
    # that build state without an HTTP request still validate.
    # ADR-038 §5.8 / ADR-039 §5.5 — load-bearing for per-tenant
    # cache key isolation when the L2 LLM Shadow is invoked.
    tenant_id: Optional[str] = None
    # Request-scoped trace ID — set at ingest, used to correlate
    # gateway calls / observability events for this run. Distinct
    # from `shadow.trace_id` (which is the ComplianceDecision's own
    # row ID generated when shadow_audit runs).
    request_trace_id: str = ""
    discrepancy: Optional[PricingDiscrepancy] = None
    rag_context: RagContext = Field(default_factory=RagContext)
    skill: Optional[SkillDocument] = None
    intent: Intent = Intent.UNKNOWN
    confidence: float = 0.0
    shadow: Optional[ComplianceDecision] = None
    selected_recipe: Optional[str] = None
    invocation: Optional[RecipeInvocation] = None
    execution_log: Optional[ExecutionLog] = None
    final_status: Optional[TerminalStatus] = None
    explanation: Optional[str] = None
    update_count: int = 0
    batch_total_variance: float = 0.0
    # Gateway integration (Phase 7)
    resolved_data: Dict[str, Any] = Field(default_factory=dict)
    effect_results: List[GatewayResponse] = Field(default_factory=list)
    # Verdict Pillar 1 (2026-04-22 compliance workshop): audit-bearing
    # upstream context — gateway-fetched evidence (matched POs,
    # warehouse snapshots, contract refs, SAP doc numbers) that the
    # operator reviews to authorise an action. Distinct from
    # `resolved_data` (transient recipe input) in both semantics and
    # lifetime: this bag survives to `ExceptionRecord.enrichment_context`
    # and is consumed by the `build_analysis` composition node to
    # populate Layer-2 evidence on the UI. Empty means "not fetched
    # for this resolution path" — the UI must render "Context Not
    # Required for Resolution", never a dash (workshop §Pillar 3).
    enrichment_context: Dict[str, Any] = Field(default_factory=dict)
    # LLM provenance (V1 PR-1 — populated only when a remote LLM
    # backend served at least one trio call; empty otherwise). Each
    # entry is one provider call (intent / recipe / shadow). Surfaces
    # provider, model_id, request_id, token usage, cache hits, cost
    # estimate, and fallback reason for SOX-grade audit. Read by the
    # Tracer at terminal-state emit time and forwarded to LangFuse as
    # generation spans (one per call).
    llm_call_traces: List["LLMCallTrace"] = Field(default_factory=list)
    # ADR-027 Phase B — per-node executed-trace evidence. Each node
    # appends one `ExecutedNode` entry as it runs; `build_analysis`
    # serialises the list into `trace_data["executed_nodes"]` so the
    # UI's EventsTimeline + PipelineDAG can reconstruct the path the
    # record took. Reanalysis preserves prior attempts on
    # `ReanalysisHistoryEntry.executed_nodes` rather than overwriting.
    execution_trace: List["ExecutedNode"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _explanation_for_terminal_state(self) -> "GraphState":
        if self.final_status and not self.explanation:
            self.explanation = f"Workflow terminated with status: {self.final_status.value}"
        return self


# ---------------------------------------------------------------------------
# ADR-027 Phase B — per-node execution evidence
# ---------------------------------------------------------------------------
#
# Domain model for the `executed_nodes` audit-bearing surface. Lives in
# contracts (not api/) so the orchestration layer can append entries
# without depending on api/. The api/schemas.py module re-exports these
# names so OpenAPI consumers see them under the API surface they're
# served from.
#
# Field semantics: see the docstrings on each model. A single ExecutedNode
# represents one node's traversal within one LangGraph invoke(). The list
# is ordered by invocation; each entry's `entered_at` is monotonic in
# practice (the orchestrator runs nodes serially per record).


class GatewayCallSpan(BaseModel):
    """Per-gateway sub-span emitted by `resolve_dependencies` (Phase B).

    `resolve_dependencies` fans gateway calls out via concurrent.futures.
    The single `ExecutedNode.duration_ms` is the wall-clock fan-out span;
    per-gateway timing lives here so the timeline can render a nested
    expand without the DAG view having to render N sub-nodes (which would
    diverge from the orchestration topology — the DAG renders ONE node,
    not N).
    """

    model_config = ConfigDict(extra="forbid")

    gateway: str
    started_at: str  # ISO-8601
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    status: Literal["ok", "error", "timeout"]


class ExecutedNode(BaseModel):
    """Per-node execution evidence (Phase B).

    Appended by every orchestration node to `state.execution_trace`.
    Persisted by `build_analysis` into `trace_data["executed_nodes"]`
    and surfaced on `TraceResponse.executed_nodes` for the UI's
    EventsTimeline + PipelineDAG.

    Reanalysis: each invoke produces a fresh list; reanalysis preserves
    prior attempts on `ReanalysisHistoryEntry.executed_nodes` rather
    than overwriting trace_data — the SOX surface cannot accept the
    audit-evidence loss.

    Field semantics:
      - `node`           — canonical orchestration node name
      - `entered_at`     — ISO-8601, node start
      - `completed_at`   — ISO-8601, node end (None if errored)
      - `duration_ms`    — wall-clock; for resolve_dependencies the
                            fan-out span — sub_spans hold per-gateway
      - `timestamp`      — convenience top-level (matches existing trace
                            style) — always equals `entered_at`
      - `status`         — completed / halted / errored
      - `decision`       — node-specific payload: intent+confidence on
                            classify, recipe on select_recipe, verdict
                            on shadow_audit, etc.
      - `exit_verdict`   — the verdict label that drove the next route
                            (`green` | `red` | `yellow` | `breach` |
                             `cross_check_disagreement` | `ok` | …)
                            None for nodes whose exit isn't a
                            conditional gate (e.g. ingest, load_skill).
      - `policy_hits`    — populated on shadow_audit; [] elsewhere
      - `sub_spans`      — populated on resolve_dependencies; [] elsewhere
    """

    model_config = ConfigDict(extra="forbid")

    node: str
    entered_at: str
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    timestamp: str
    status: Literal["completed", "halted", "errored"]
    decision: Dict[str, Any] = Field(default_factory=dict)
    exit_verdict: Optional[str] = None
    policy_hits: List[str] = Field(default_factory=list)
    sub_spans: List[GatewayCallSpan] = Field(default_factory=list)


GraphState.model_rebuild()
