"""Request/Response Pydantic models for the ASOE API.

Maps to architecture_v3.md Section 8.2 endpoint contracts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from contracts.models import ExecutedNode, GatewayCallSpan, OrderEvent

# Re-exported so OpenAPI consumers see ExecutedNode / GatewayCallSpan
# under api.schemas. The domain definitions live in contracts/models.py
# (orchestration appends to state.execution_trace and must not import
# api/).
__all__ = ["ExecutedNode", "GatewayCallSpan"]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ResolveRequest(OrderEvent):
    """POST /api/v1/exceptions/resolve — same fields as OrderEvent."""

    pass


class DispositionRequest(BaseModel):
    """PATCH /api/v1/exceptions/{id}/disposition — unified HITL primitive
    (v2 consolidation).

    Single endpoint collapsing Approve + Reject + Override into a
    sub-type-discriminated resolution. The caller specifies the chosen
    action; the server derives the sub-type:
      - chosen_action == recommended_action → APPROVE (exceptions:approve)
      - chosen_action == "NO_ACTION"        → REJECT  (exceptions:approve)
      - chosen_action != recommended_action → OVERRIDE (exceptions:override,
                                                         four-eyes applies)

    Notes are mandatory (SOX). reason_tag is validated against
    AllowedOverrideReasonTag when present; it defaults to "other" for
    Phase 2 compatibility. Emits a single EXCEPTION_RESOLVED audit event
    with sub_type in new_value so downstream analytics stay consistent.
    """

    model_config = ConfigDict(extra="forbid")

    action: str  # validated against AllowedResolutionAction ∪ {NO_ACTION}
    notes: str  # mandatory (SOX)
    reason_tag: str  # required (Phase 3); validated against AllowedOverrideReasonTag


class CosignRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/override/cosign — second-reviewer decision
    on a pending high-value override (Phase 2 four-eyes control).

    ``approve=True`` applies the pending override (lifecycle → RESOLVED);
    ``approve=False`` rejects it and restores the prior lifecycle. Notes
    are mandatory (SOX) in both cases.

    The cosigner's identity comes from the authenticated user and must
    differ from the initiator — same-person cosign is a SOD violation.
    """

    model_config = ConfigDict(extra="forbid")

    approve: bool
    notes: str


class EscalateRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/escalate — dedicated escalation routing.

    Decoupled from Override: escalation changes routing (lifecycle →
    ESCALATED) without asserting a resolution action. ``reason`` is a
    mandatory SOX justification (min length 1). ``to_role`` optionally
    names the target role for routing metadata only — policy and state
    machine do not branch on it.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1)
    to_role: Optional[Literal["admin", "manager"]] = None


class ChallengeRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/challenge — post-execution challenge.

    Available on RESOLVED exceptions (GREEN verdict post-execution review).
    Transitions RESOLVED → ESCALATED for investigation.
    """

    challenge_reason: str


class AdminReleaseRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/admin-release — admin release of RED-blocked exception.

    Available on BLOCKED exceptions (RED verdict). Admin-only.
    Transitions BLOCKED → PENDING_ADMIN_REVIEW for admin to select action.
    """

    release_reason: str
    risk_acknowledgment: bool  # must be True


class ReanalyzeRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/reanalyze — human-triggered graph replay.

    Allowed only on YELLOW/RED verdicts or FAILED lifecycle. The request
    re-runs the full graph (including a fresh Compliance Shadow) and
    appends the prior and new outcome to reanalysis_history (append-only).

    A mandatory free-text `reason` is required for SOX audit traceability.
    Rate-limited by REANALYSIS_MAX_ATTEMPTS (contracts/policy.py) to prevent
    outcome-shopping.
    """

    reason: str


class PolicyUpdateRequest(BaseModel):
    """PUT /api/v1/policies/{tenant_id} — update policy overrides."""

    policy_key: str
    value: Any
    change_reason: Optional[str] = None


class WorkflowRequest(BaseModel):
    """POST /api/v1/workflows — multi-step workflow execution."""

    workflow_id: str
    name: str
    steps: List[WorkflowStepRequest]
    base_event: ResolveRequest


class WorkflowStepRequest(BaseModel):
    step_id: str
    intent: str
    description: str
    input_mapping: Dict[str, str] = Field(default_factory=dict)
    compensation_recipe: Optional[str] = None


# Rebuild WorkflowRequest now that WorkflowStepRequest is defined
WorkflowRequest.model_rebuild()


class LoginRequest(BaseModel):
    """POST /api/auth/login — email/password auth."""

    email: str
    password: str


class MFAVerifyRequest(BaseModel):
    """POST /api/auth/mfa/verify — TOTP verification."""

    mfa_token: str
    code: str


class RefreshRequest(BaseModel):
    """POST /api/auth/refresh — token refresh."""

    refresh_token: str


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """GET /api/v1/health"""

    status: str = "ok"
    version: str
    kill_switch: bool
    explain_mode: bool
    allowed_intents: List[str]
    lifecycle_states: List[str]
    allowed_recipes: List[str]
    allowed_resolution_actions: List[str]
    allowed_override_reason_tags: List[str]
    # Per-intent override-reason vocabulary. Same keys as allowed_intents;
    # values are the subset of allowed_override_reason_tags that apply to
    # each intent. Seeded with the global set for every intent today —
    # product/compliance will curate these in a follow-up. Consumed by
    # the UI Override chooser to narrow its options by record.intent.
    allowed_override_reason_tags_by_intent: Dict[str, List[str]]


class ResolveResponse(BaseModel):
    """Response for synchronous resolve and explain endpoints."""

    exception_id: str
    trace_id: Optional[str] = None
    intent: Optional[str] = None
    shadow_verdict: Optional[str] = None
    selected_recipe: Optional[str] = None
    final_status: Optional[str] = None
    explanation: Optional[str] = None
    execution_log: Optional[Dict[str, Any]] = None
    effect_results: List[Dict[str, Any]] = Field(default_factory=list)


class ExceptionSummary(BaseModel):
    """Single exception in a list response."""

    id: str
    tenant_id: str
    order_id: str
    event_type: str
    intent: Optional[str] = None
    lifecycle_state: str
    shadow_verdict: Optional[str] = None
    selected_recipe: Optional[str] = None
    final_status: Optional[str] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    created_at: str
    updated_at: str


class ExceptionListResponse(BaseModel):
    """GET /api/v1/exceptions — paginated list."""

    data: List[ExceptionSummary]
    cursor: Optional[str] = None
    has_more: bool = False


class ExceptionDetailResponse(BaseModel):
    """GET /api/v1/exceptions/{id} — full detail."""

    id: str
    tenant_id: str
    order_id: str
    event_type: str
    intent: Optional[str] = None
    lifecycle_state: str
    shadow_verdict: Optional[str] = None
    selected_recipe: Optional[str] = None
    final_status: Optional[str] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    trace_id: Optional[str] = None
    resolution_data: Dict[str, Any] = Field(default_factory=dict)
    resolved_by: Optional[str] = None
    resolved_action: Optional[str] = None
    resolution_notes: Optional[str] = None
    reanalysis_history: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str


class SAPActionStep(BaseModel):
    """A recommended SAP action step.

    Human-facing only — not machine-consumed. Rendered verbatim in Layer 2
    for operator reference. Constrained generation is NOT required here
    (CLAUDE.md §3).
    """
    transaction: str  # e.g., "VA02", "VK11"
    table: Optional[str] = None  # e.g., "VBAK", "KONV"
    field: Optional[str] = None  # e.g., "LIFSK", "KBETR"
    description: str


class TraceResponse(BaseModel):
    """GET /api/v1/exceptions/{id}/trace — full TraceRecord JSON."""

    trace_id: str
    event_id: str
    skill_name: Optional[str] = None
    intent_selected: Optional[str] = None
    shadow_verdict: Optional[str] = None
    shadow_policy_hits: List[str] = Field(default_factory=list)
    recipe_name: Optional[str] = None
    constrained_output_schemas: Dict[str, str] = Field(default_factory=dict)
    gateway_calls: List[str] = Field(default_factory=list)
    backend_fallback: Optional[str] = None
    is_fallback_generated: bool = False
    final_status: Optional[str] = None
    explanation: Optional[str] = None

    # ── Human-facing structured narrative (Layer 2 enrichment) ────────
    # All optional. Populated by the recipe layer when available; the UI
    # renders whichever fields are present. None of these are consumed by
    # code downstream — constrained generation is not required.
    narrative: Optional[str] = None
    """Multi-paragraph human explanation of what the agent did and why."""

    resolution_steps: List[str] = Field(default_factory=list)
    """Ordered, actionable steps the operator should confirm / perform."""

    sap_actions: List[SAPActionStep] = Field(default_factory=list)
    """Recommended SAP transaction-level steps (T-codes, tables, fields)."""

    customer_email_draft: Optional[str] = None
    """Copy-paste-ready customer communication draft."""

    # Verdict Pillar 2.3 (2026-04-22 workshop) — structured audit-gap
    # surface. When the build_analysis node flags
    # AUDIT_CONTEXT_MISSING, these fields carry the class name +
    # ordered list of missing audit-bearing fields so auditors don't
    # have to regex the free-text explanation. Both None when
    # coverage was complete.
    audit_context_missing_class: Optional[str] = None
    """Pydantic class name whose audit-bearing fields were incomplete
    (e.g. "PriceHoldAnalysisData"). None = coverage OK."""

    audit_context_missing_fields: List[str] = Field(default_factory=list)
    """Ordered list of field names declared audit-bearing in
    compliance/audit_bearing_registry.yaml that could not be
    populated for this record. Empty = coverage OK."""

    # ADR-027 Phase B — per-node executed-trace evidence.
    executed_nodes: List[ExecutedNode] = Field(default_factory=list)
    """Ordered list of nodes that ran for this trace, with timing,
    decision payload, and exit verdict. Sourced from
    `state.execution_trace` and persisted into
    `trace_data["executed_nodes"]`. Empty for traces written before
    Phase B's instrumentation landed."""


class StatsResponse(BaseModel):
    """GET /api/v1/exceptions/stats — dashboard metrics."""

    total_exceptions: int = 0
    open_exceptions: int = 0
    auto_resolved: int = 0
    manual_review: int = 0
    blocked: int = 0
    failed: int = 0
    avg_resolution_time_seconds: Optional[float] = None
    by_intent: Dict[str, int] = Field(default_factory=dict)
    by_lifecycle_state: Dict[str, int] = Field(default_factory=dict)
    by_shadow_verdict: Dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# ADR-027 — pipeline visualization (Phase A topology, Phase B trace)
# ---------------------------------------------------------------------------
#
# Topology surface (Phase A): authentication-required GET
# /api/v1/pipeline/topology returns the compiled-graph topology + A.0
# verdict labels. The schema is intentionally narrow — no per-record
# data, just the graph shape — so it can be cached aggressively by
# `topology_hash`.
#
# Per-record execution evidence (Phase B): `ExecutedNode` lives on the
# extended trace and carries the verdict that actually fired for each
# record's traversal. Reanalysis attempts are captured in the typed
# `ReanalysisHistoryEntry` so prior paths' per-node audit evidence is
# preserved when subsequent reanalyses overwrite `trace_data`.


class PipelineTopologyNode(BaseModel):
    """A node in the pipeline topology (Phase A)."""

    id: str          # canonical orchestration node name
    label: str       # human-readable; today same as id
    kind: Literal["node", "terminal"]


class PipelineTopologyEdge(BaseModel):
    """A directed edge in the pipeline topology (Phase A).

    `verdict_label` is populated for every conditional edge — both the
    explicit ones (registered in `_VERDICT_LABELS`) and the implicit
    classify-time disagreement gate (registered in
    `_IMPLICIT_VERDICT_LABELS`). Unconditional edges carry
    `verdict_label=None`.

    A single compiled-graph conditional edge can produce multiple
    rows when one route key (e.g. `terminal`) corresponds to multiple
    verdicts (e.g. RED + YELLOW both terminate `shadow_audit`). The
    introspection helper expands accordingly; the DAG renderer draws
    each as a distinct labelled edge.
    """

    from_node: str
    to_node: str
    conditional: bool
    verdict_label: Optional[str] = None


class PipelineTopology(BaseModel):
    """Response shape for GET /api/v1/pipeline/topology (Phase A).

    `topology_hash` is a stable SHA-256 over the canonical JSON of
    (nodes, edges); the UI caches by hash and revalidates on
    `useHealth` polling tick (ADR-027 Open Question §1).
    """

    topology_hash: str
    nodes: List[PipelineTopologyNode]
    edges: List[PipelineTopologyEdge]


class ReanalysisHistoryEntry(BaseModel):
    """Typed replacement for the legacy `List[Dict[str, Any]]` reanalysis_history (Phase B).

    Each entry captures one reanalysis attempt's snapshot AND its
    `executed_nodes` list, so the prior path's per-node audit
    evidence is preserved when the next attempt overwrites
    `trace_data`. Without this, reanalysing a record destroys
    audit evidence — unacceptable on the SOX surface.

    The legacy untyped shape (List[Dict[str, Any]]) remains the
    persisted form on records written before Phase B; the API
    layer projects untyped entries by populating only the
    `prior_*` / `new_*` scalars and leaving `executed_nodes=[]`
    with the documented "pre-Phase-B" banner surfaced by the UI.
    """

    model_config = ConfigDict(extra="forbid")

    attempt: int
    attempted_at: str
    attempted_by: str
    reason: Optional[str] = None
    prior_trace_id: str
    prior_shadow_verdict: Optional[str] = None
    prior_final_status: Optional[str] = None
    prior_lifecycle_state: str
    new_trace_id: str
    new_shadow_verdict: Optional[str] = None
    new_final_status: Optional[str] = None
    new_lifecycle_state: str
    executed_nodes: List[ExecutedNode] = Field(default_factory=list)


class AsyncResolveResponse(BaseModel):
    """POST /api/v1/exceptions/resolve/async — queued task."""

    task_id: str
    status: str = "queued"


class AuthTokenResponse(BaseModel):
    """Auth response with JWT tokens."""

    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    user: Optional[UserProfile] = None
    mfa_required: bool = False
    mfa_token: Optional[str] = None


class UserProfile(BaseModel):
    """GET /api/auth/me — current user profile.

    visible_tabs and permissions are computed from roles — never stored per user.
    assigned_accounts drives server-side customer scope filtering.
    """

    sub: str
    email: str
    name: str
    title: Optional[str] = None
    avatar_initials: Optional[str] = None
    roles: List[str]
    org: str
    permissions: List[str]
    assigned_accounts: List[str] = Field(default_factory=list)
    visible_tabs: List[str] = Field(default_factory=list)


class AccountResponse(BaseModel):
    """A retail customer account within a tenant."""

    id: str
    name: str
    tenant_id: str
    bp_number: str
    tier: str
    region: Optional[str] = None


class AccountListResponse(BaseModel):
    """GET /api/v1/accounts — list of accounts for the tenant."""

    data: List[AccountResponse]


# Rebuild AuthTokenResponse now that UserProfile is defined
AuthTokenResponse.model_rebuild()


class PolicyOverrideResponse(BaseModel):
    """Response for policy update."""

    id: str
    tenant_id: str
    policy_key: str
    value: Any
    effective_from: str
    created_by: str


# ---------------------------------------------------------------------------
# D3/D4 — Line-item and analysis response models
# ---------------------------------------------------------------------------


class LineItem(BaseModel):
    """Single line item in an exception."""

    line_id: str
    sku: str
    description: str
    uom: str
    quantity: int
    erp_price: float
    po_price: float
    root_cause: Optional[str] = None


class LineItemsResponse(BaseModel):
    """GET /api/v1/exceptions/{id}/line-items"""

    data: List[LineItem]


class PricingWaterfallStep(BaseModel):
    """A single step in the pricing waterfall analysis."""

    type: str
    label: str
    record: Optional[str] = None
    value: Optional[float] = None
    running: Optional[float] = None
    detail: Optional[str] = None
    error: Optional[str] = None


class LineAnalysis(BaseModel):
    """Analysis details for a single line item."""

    line_id: str
    diagnosis: str
    resolution: str
    risk: str
    waterfall: List[PricingWaterfallStep] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Analysis enrichment payloads (review L2)
#
# Each enrichment type mirrors one recipe's output shape as projected into
# a UI-consumable schema. The adapter in `api/analysis_adapters.py` maps
# `record.resolution_data` + `record.original_event` + policy constants
# into these models; the /analysis endpoint then surfaces them as optional
# fields on AnalysisResponse. Field names intentionally match the UI's
# `OrderAnalysis` interface (`src/types/exceptions.ts`) so the
# data-presence rendering pattern on the UI side needs zero changes.
#
# IMPORTANT: these Pydantic classes are NOT a second source of truth for
# recipe output. Recipes continue to return plain dicts; the adapter is
# the single projection point. Adding a new enrichment => add (a) a
# Pydantic model here, (b) an adapter function, (c) an optional field
# below on AnalysisResponse. No recipe changes.
# ---------------------------------------------------------------------------


class PriceHoldAnalysisData(BaseModel):
    """PriceHoldReleaseRecipe → UI `price_hold_analysis`.

    `hold_status` is a two-valued projection of the recipe's four-valued
    `status`: "RELEASED" when the hold was lifted, "HELD" otherwise (the
    recipe's REVIEW_REQUIRED, REJECTED, and FAILED outcomes all leave the
    hold in place). Other fields mirror the recipe output or the event
    inputs (po_price / sap_base_price) / policy constants
    (tolerance_pct / hard_block_pct).
    """

    model_config = ConfigDict(extra="forbid")

    hold_status: Literal["HELD", "RELEASED"]
    po_price: float
    sap_base_price: float
    variance_pct: float
    tolerance_pct: float
    hard_block_pct: float
    action: Literal["AUTO_RELEASE", "ESCALATE", "HARD_BLOCK"]
    reason: str


class EdiMismatchAnalysisData(BaseModel):
    """EdiMismatchRecipe → UI `edi_mismatch_analysis`.

    `sub_type` is intentionally untyped-string (not a Literal) so the UI
    can render new sub_types added in the recipe without a contract
    bump. `expected_value` / `received_value` are `Any` because EDI 850
    line fields are heterogeneous (SKU strings, qty integers, ship-to
    dicts).

    Note: PRICE_MISMATCH never reaches this recipe — the classifier
    routes it to CONTRACTUAL_CORRECTION / PriceAdjustmentRecipe.py to
    preserve the single-source-of-truth invariant. The adapter returns
    None for FAILED recipe outputs, so PRICE_MISMATCH routing-error
    records never surface this field.
    """

    model_config = ConfigDict(extra="forbid")

    sub_type: str
    classification: Literal["HARD_REJECT", "REVIEW", "ESCALATE"]
    recommended_action: str
    autonomy_level: Literal["L1", "L2", "L3"]
    expected_value: Any = None
    received_value: Any = None
    notification_template: Optional[str] = None


class EmailOrderEntryFloorStatus(BaseModel):
    """The four "non-disable-able floor" checks (ADR-034 §4).

    Each boolean is the GREEN/RED gate evidence the operator reviews
    when authorising an email-channel order. Adapter populates these
    from `record.enrichment_context.{sender_auth_context,
    customer_resolution_context, duplicate_po_pre_check_context,
    credit_check_context}` — the four `email_intake` gateway
    operations declared on the recipe spec — and falls back to
    `event.metadata.non_disableable_floor` defensively when a
    gateway response is empty.

    Compliance Pillar 1: even on a RED-shadowed record these fields
    must be populated (gateway READS run before shadow_audit per
    ADR-025).
    """

    model_config = ConfigDict(extra="forbid")

    sender_authorized: bool
    customer_resolved: bool
    duplicate_po_clear: bool
    credit_clear: bool


class EmailOrderEntryAnalysisData(BaseModel):
    """EmailOrderEntryRecipe → UI `email_order_entry_analysis`
    (ADR-034 Phase B).

    Mirrors `asoe-ui/src/types/exceptions.ts::EmailOrderEntryAnalysisData`.
    `classification` is the recipe's confidence-band output;
    `recommended_action` is constrained by `AllowedResolutionAction`
    on the wire (intentionally `str` here so the UI section can render
    new actions added downstream without a contract bump — same pattern
    as EdiMismatchAnalysisData).

    `reject_reason_code` is conditional on classification == FATAL_REJECT;
    None on every other classification (Pillar 3 conditional field —
    rendered as "Context Not Required for Resolution" by EvidenceBlock
    when the predicate doesn't hold).
    """

    model_config = ConfigDict(extra="forbid")

    composite_confidence: float
    classification: Literal[
        "ONE_CLICK_APPROVE", "STANDARD_REVIEW", "LOW_CONFIDENCE", "FATAL_REJECT",
    ]
    recommended_action: str
    autonomy_level: Literal["L1", "L2", "L3", "L4"]
    validation_failures: List[str] = []
    floor_breaches: List[str] = []
    reject_reason_code: Optional[
        Literal[
            "sender_unauthorized", "customer_unresolved",
            "duplicate_po_confirmed", "credit_block",
            "corrupt_input", "policy_floor_breach",
        ]
    ] = None
    floor_status: EmailOrderEntryFloorStatus
    notification_template: Optional[str] = None


class AlternateDeliveryOption(BaseModel):
    """One ranked alternate delivery option from
    DeliveryDelayResolutionRecipe._rank_options."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str  # EXPEDITE / SPLIT_SHIP / PARTIAL / RESCHEDULE
    title: str = ""
    description: str = ""
    new_eta: Optional[str] = None
    extra_cost: float = 0.0
    recommended: bool = False


class DeliveryDelayAnalysisData(BaseModel):
    """DeliveryDelayResolutionRecipe → UI `delivery_delay_analysis`.

    Registry-classified fields (2026-04-22 workshop):

      * audit-bearing:
          planned_date, projected_eta, days_late, delay_category,
          affected_lines, at_risk, sla_deadline (when present)
      * conditional:
          alternate_options (depends_on resolved_action ∈
          {EXPEDITE, SPLIT_SHIP, PARTIAL, RESCHEDULE})
      * contextual:
          delay_reason, carrier, route, rule_id

    `at_risk` and `sla_deadline` are currently covered by the
    `delivery_delay_financial_gap` grandfather clause — the contract
    gateway that would produce them isn't wired yet. The composer
    treats them as contextual until the 2026-07-21 deadline.
    """

    model_config = ConfigDict(extra="forbid")

    planned_date: str
    projected_eta: str
    days_late: int
    delay_category: str
    affected_lines: int
    at_risk: Optional[float] = None  # grandfathered
    sla_deadline: Optional[str] = None  # grandfathered
    alternate_options: List[AlternateDeliveryOption] = Field(default_factory=list)
    # Contextual — absence on the UI is expected (structural omission
    # via EvidenceBlock in asoe-ui).
    delay_reason: Optional[str] = None
    carrier: Optional[str] = None
    route: Optional[str] = None
    rule_id: Optional[str] = None


class OverMaxLine(BaseModel):
    """One affected order line in an OVER_MAX exception."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    description: str = ""
    qty: float
    max_line_qty: Optional[float] = None
    excess: float = 0.0
    is_even_layer_item: bool = False


class TrimPlanLine(BaseModel):
    """One row in OverMaxTrimRecipe's trim plan."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    description: str = ""
    ordered: float
    trimmed_to: float
    delta: float = 0.0
    action: Literal["TRIM", "SKIP", "OK"] = "TRIM"


class OverMaxAnalysisData(BaseModel):
    """OverMaxTrimRecipe → UI `overmax_analysis`.

    Registry-classified fields (2026-04-22 workshop):
      * audit-bearing: total_ordered, max_qty, excess_qty,
        exceedance_pct, uom, at_risk, order_lines, trim_plan.
      * audit-bearing (grandfathered until 2026-07-21 — gateway gap):
        contract_ref, block_status, block_reason.

    The recipe computes excess_qty / exceedance_pct / trim_plan /
    at_risk from event metadata; the SAP block + contract gateway
    that would supply contract_ref / block_status / block_reason
    is not yet wired (overmax_gateway_gap clause).
    """

    model_config = ConfigDict(extra="forbid")

    total_ordered: float
    max_qty: float
    excess_qty: float
    exceedance_pct: float
    uom: str = ""
    at_risk: float = 0.0
    order_lines: List[OverMaxLine] = Field(default_factory=list)
    trim_plan: List[TrimPlanLine] = Field(default_factory=list)
    # Grandfathered audit-bearing — populated when the SAP contract
    # gateway lands.
    contract_ref: Optional[str] = None
    block_status: Optional[str] = None
    block_reason: Optional[str] = None


class RoundUpPlanLine(BaseModel):
    """One row in MOQRoundUpRecipe's round-up plan."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    description: str = ""
    ordered: float
    round_up_to: float
    delta: float = 0.0
    action: Literal["ROUND_UP", "ACCEPT_BELOW", "ESCALATE"] = "ROUND_UP"


class MOQAnalysisData(BaseModel):
    """MOQRoundUpRecipe → UI `moq_analysis`.

    Registry-classified fields (2026-04-22 workshop):
      * audit-bearing: ordered_qty, moq_qty, shortfall_qty,
        shortfall_pct, sku, unit_cost, uom, at_risk, round_up_plan.
      * grandfathered audit-bearing (until 2026-07-21 — gateway gap):
        moq_source, channel, contract_ref, block_status.
      * contextual: description, block_message.

    `at_risk` is sourced from the recipe's `uplift_value`
    (uplift_qty × unit_cost). The `sap_steps` UI field is omitted
    here — it's contextual / not produced by the recipe and the
    UI can render whatever's present in `round_up_plan`.
    """

    model_config = ConfigDict(extra="forbid")

    ordered_qty: float
    moq_qty: float
    shortfall_qty: float
    shortfall_pct: float
    sku: str
    unit_cost: float = 0.0
    uom: str = ""
    at_risk: float = 0.0
    round_up_plan: List[RoundUpPlanLine] = Field(default_factory=list)
    # Grandfathered audit-bearing.
    moq_source: Optional[str] = None
    channel: Optional[str] = None
    contract_ref: Optional[str] = None
    block_status: Optional[str] = None
    # Contextual.
    description: Optional[str] = None
    block_message: Optional[str] = None


class PalletLine(BaseModel):
    """One per-line pallet alignment row from PalletAlignmentRecipe."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    description: str = ""
    uom: str = ""
    layer_qty: float = 0.0
    pallet_qty: float = 0.0
    ordered_qty: float
    complete_layers: int = 0
    loose_qty: float = 0.0
    full_pallets: int = 0
    pallet_fill_pct: float = 0.0
    violation_type: Optional[str] = None


class PalletSuggestion(BaseModel):
    """One AI suggestion row from PalletAlignmentRecipe."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    description: str = ""
    current: float
    suggested: float
    delta: float = 0.0
    layers: int = 0
    full_pallets: int = 0
    reason: str = ""


class PalletAnalysisData(BaseModel):
    """PalletAlignmentRecipe → UI `pallet_analysis`.

    Registry-classified fields (2026-04-22 workshop):
      * audit-bearing: total_ordered_cases, loose_cases_total,
        order_line_count, classification, suggested_plan, lines.

    Recipe + UI line/plan shapes are 1:1, so the adapter is purely
    coercion. The UI's mock-only legacy top-level fields
    (at_risk_total, extra_labor_est_hrs, freight_waste_pct) are not
    in this contract — they're not classified in the registry and
    no recipe currently produces them. Those are kept optional on
    the UI type and remain mock-only.
    """

    model_config = ConfigDict(extra="forbid")

    total_ordered_cases: float
    loose_cases_total: float
    order_line_count: int
    classification: str
    lines: List[PalletLine] = Field(default_factory=list)
    suggested_plan: List[PalletSuggestion] = Field(default_factory=list)


class PriceAnalysisData(BaseModel):
    """PriceAdjustmentRecipe → UI `price_analysis`.

    Registry-classified fields (2026-04-22 workshop, post-T4 retirement
    of price_analysis_gateway_gap):
      * audit-bearing (event/control): erp_unit_price, po_unit_price,
        variance_amount, variance_pct, total_at_risk, total_quantity,
        uom, sku.
      * audit-bearing (gateway, sap_doc): doc_type, doc_number.
      * audit-bearing (gateway, sap_contract): contract_ref, rule_id.
      * audit-bearing (gateway, promotion): promotion_ref,
        root_cause_category.
      * contextual: material_desc, order_date.

    Sources:
      * `record.original_event` — po_price, sap_base_price, line_item,
        retailer_id, sku, line_count, metadata.
      * `record.enrichment_context["sap_doc_context"]` — SAP document
        metadata (doc_type, doc_number, applied condition chain).
      * `record.enrichment_context["contract_context"]` — KONA / custom
        contract lookup (contract_ref, rule_id_hints).
      * `record.enrichment_context["promotion_context"]` — promotion
        master (promotion_ref, root_cause_category).
    """

    model_config = ConfigDict(extra="forbid")

    erp_unit_price: float
    po_unit_price: float
    variance_amount: float
    variance_pct: float
    total_at_risk: float
    total_quantity: float
    uom: str
    doc_type: str
    doc_number: str
    sku: str
    rule_id: str
    root_cause_category: str
    # Contextual — present when the line is governed by an active
    # contract or promotion; absent otherwise (no fallback "—").
    contract_ref: Optional[str] = None
    promotion_ref: Optional[str] = None
    material_desc: Optional[str] = None
    order_date: Optional[str] = None


class WarehouseInfo(BaseModel):
    """Inventory snapshot for one DC. Audit-bearing per registry::
    BackOrderAnalysisData.primary_dc — reviewer attests against the
    plant + qty before approving an ALT_DC or SUBSTITUTE action."""

    model_config = ConfigDict(extra="forbid")

    plant: str
    name: str = ""
    region: str = ""
    qty: float


class AlternateWarehouse(BaseModel):
    """Alternate-DC option with shipping economics. Conditional on
    `resolved_action == ALT_DC` per the registry."""

    model_config = ConfigDict(extra="forbid")

    plant: str
    name: str = ""
    region: str = ""
    qty: float
    eta_days: int
    freight_delta_per_unit: float
    freight_delta_total: float


class SubstituteSKU(BaseModel):
    """Substitute SKU candidate. Conditional on
    `resolved_action == SUBSTITUTE` per the registry."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    description: str = ""
    available_qty: float
    price_delta_pct: float
    acceptance_rate: float
    source: str = ""
    priority: int = 0


class InboundOrder(BaseModel):
    """Inbound production / PO entry. Conditional on
    `resolved_action == RESCHEDULE` per the registry."""

    model_config = ConfigDict(extra="forbid")

    qty: float
    date: Optional[str] = None
    eta: Optional[str] = None
    po_num: Optional[str] = None


class ResolutionOptionScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: float = 0.0
    revenue: float = 0.0
    logistics: float = 0.0
    preference: float = 0.0


class ResolutionOption(BaseModel):
    """One ranked resolution option for a back-order. Audit-bearing per
    registry::BackOrderAnalysisData.resolution_options."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str  # SPLIT_SHIPMENT / ALT_DC / SUBSTITUTE / RESCHEDULE
    title: str = ""
    description: str = ""
    composite_score: float = 0.0
    scores: ResolutionOptionScores = Field(default_factory=ResolutionOptionScores)
    sap_steps: List[str] = Field(default_factory=list)


class BackOrderAnalysisData(BaseModel):
    """BackOrderResolutionRecipe → UI `backorder_analysis`.

    Registry-classified fields (2026-04-22 workshop):
      * audit-bearing: ordered_qty, available_qty, gap_qty, gap_pct,
        unit_price, uom, at_risk, atp_date, resolution_options.
      * audit-bearing (gateway): primary_dc.
      * conditional (gateway, depends_on resolved_action):
        alternate_warehouses (ALT_DC), substitutes (SUBSTITUTE),
        production / inbound_po (RESCHEDULE).

    No grandfather clause in this engagement. Gateway-dependent
    audit-bearing fields (primary_dc, atp_date) MUST persist via
    enrichment_context["inventory_snapshot"]; missing → composer
    routes to AUDIT_CONTEXT_MISSING.

    Sources:
      * `record.original_event` — recipe input metadata (ordered_qty,
        available_qty, unit_price, uom, sku).
      * `record.enrichment_context["inventory_snapshot"]` — gateway
        snapshot (primary_dc, atp_date, alternate_warehouses,
        substitutes, production, inbound_po).
      * `record.resolution_data` — recipe-computed fields (gap_qty,
        gap_pct, at_risk, resolution_options, recommended_action).
    """

    model_config = ConfigDict(extra="forbid")

    ordered_qty: float
    available_qty: float
    gap_qty: float
    gap_pct: float
    unit_price: float
    uom: str
    at_risk: float
    atp_date: str
    primary_dc: WarehouseInfo
    resolution_options: List[ResolutionOption] = Field(default_factory=list)
    # Conditional — present only when the chosen resolution path uses them.
    alternate_warehouses: List[AlternateWarehouse] = Field(default_factory=list)
    substitutes: List[SubstituteSKU] = Field(default_factory=list)
    production: Optional[InboundOrder] = None
    inbound_po: Optional[InboundOrder] = None


class OrderSnapshot(BaseModel):
    """One side of a matched-PO pair from the OMS get_matched_po_details
    gateway. All subfields are audit-bearing per the
    DuplicateDetectionData.original_order / duplicate_order entries
    in compliance/audit_bearing_registry.yaml."""

    model_config = ConfigDict(extra="forbid")

    so_number: str
    po_number: str
    created_date: str
    total_value: float
    line_count: int
    status: str


class DuplicateDetectionData(BaseModel):
    """DuplicatePORecipe → UI `duplicate_detection`.

    Registry-classified fields (2026-04-22 workshop):

      * audit-bearing (gateway): original_order, duplicate_order
        (OrderSnapshot pair from oms/get_matched_po_details).
      * audit-bearing (control): days_between, cancellation_target,
        autonomy_applied.
      * audit-bearing (recipe-output): confidence, recommended_action.
      * contextual: detection_method (regenerable from signal_scores).

    No grandfather clause: every audit-bearing field must persist
    end-to-end. Empty enrichment_context routes to
    AUDIT_CONTEXT_MISSING via the build_analysis composer.

    Sources:
      * `record.enrichment_context["matched_po_details"]` — gateway
        OrderSnapshot pair + days_between + detection_method +
        cancellation_target.
      * `record.resolution_data` — recipe composite_score (→
        confidence), recommended_action, autonomy_level.
    """

    model_config = ConfigDict(extra="forbid")

    original_order: OrderSnapshot
    duplicate_order: OrderSnapshot
    detection_method: Optional[str] = None  # contextual
    days_between: int
    confidence: float  # 0-100
    recommended_action: str
    cancellation_target: str
    autonomy_applied: str


class ComparisonLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    description: str = ""
    qty: float
    unit_price: float


class ComparisonOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    so_number: str
    po_number: str
    created_date: str
    customer: str = ""
    lines: List[ComparisonLineItem] = Field(default_factory=list)
    total_value: float
    status: str


class OrderComparisonData(BaseModel):
    """Synthesised side-by-side comparison from the same
    `matched_po_details` payload that drives DuplicateDetectionData.
    No dedicated recipe or gateway — single source of truth (R5).

    Per the registry (OrderComparisonData entry, "Synthesised from
    DuplicateDetection; same attestation target"), enforcement of
    audit-bearing coverage is delegated to DuplicateDetectionData.
    This adapter is best-effort — projects what's present in
    matched_po_details.
    """

    model_config = ConfigDict(extra="forbid")

    orders: List[ComparisonOrder] = Field(default_factory=list)
    matching_fields: List[str] = Field(default_factory=list)
    differing_fields: List[str] = Field(default_factory=list)


class EntityProfile(BaseModel):
    """Master-data context for the exception's customer entity.

    Mirrors `EntityProfile` in `asoe-ui/src/types/exceptions.ts`.
    Composed by `api.profile_composer.compose_entity_profile` from a
    seed `Account` lookup; tier / VIP / credit-standing fields are
    optional today (no producer wired) and tracked under the
    `EntityProfile` grandfather clause in the audit-bearing registry.
    """

    customer_name: str
    bp_number: str
    customer_tier: Optional[str] = None
    vip_status: Optional[bool] = None
    credit_standing: Optional[str] = None
    location: Optional[str] = None
    region: Optional[str] = None


class ImpactMetrics(BaseModel):
    """Quantitative blast radius of the exception.

    Mirrors `ImpactMetrics` in `asoe-ui/src/types/exceptions.ts`.
    Composed deterministically from line-item totals and record
    metadata. SLA-priority is rendered as a string so the UI can
    map verdicts → display variants without a hardcoded enum
    (Guardrail #2 on the UI side).
    """

    revenue_at_risk: float
    delta_amount: float
    delta_percentage: float
    fulfillment_gap_pct: Optional[float] = None
    sla_priority: str
    sla_deadline: Optional[str] = None
    affected_lines: int


class AnalysisResponse(BaseModel):
    """GET /api/v1/exceptions/{id}/analysis"""

    diagnosis: str
    confidence: int
    risk: str
    resolution: str
    lines: List[LineAnalysis] = Field(default_factory=list)

    # ── Order-level narrative fields ────────────────────────────────
    # Mirrors `OrderAnalysis` in `asoe-ui/src/types/exceptions.ts`.
    # These are the prose layer the UI's `AgentAnalysisSection` reads
    # for the "Root Cause" and "Recommendation" blocks. Both are
    # optional because (a) the composer fills them only when there is
    # enough recipe / shadow context to do so honestly, and (b) the
    # operator must be able to distinguish "agent had no narrative"
    # from a fabricated default — Verdict 2026-04-22 partial-truth
    # guard. Empty → UI structurally omits the block.
    root_cause: Optional[str] = None
    recommendation: Optional[str] = None

    # ── Entity / impact context (Verdict 2026-04-22 commitment) ─────
    # The two-pane Layer-2 evidence the UI's `ContextStrip` renders:
    # entity master data (Account lookup) + the quantitative blast
    # radius. The composer at `api.profile_composer` is the single
    # source of truth for these projections — recipes do not assemble
    # them. Either field absent → UI suppresses its column.
    entity_profile: Optional[EntityProfile] = None
    impact_metrics: Optional[ImpactMetrics] = None

    # Data-presence enrichment fields (review L2). Populated by
    # `api.analysis_adapters.ANALYSIS_ADAPTERS` keyed on
    # `record.selected_recipe`. Absent when the recipe output is
    # malformed, FAILED, or the record has no recipe yet.
    price_hold_analysis: Optional[PriceHoldAnalysisData] = None
    edi_mismatch_analysis: Optional[EdiMismatchAnalysisData] = None
    delivery_delay_analysis: Optional[DeliveryDelayAnalysisData] = None
    overmax_analysis: Optional[OverMaxAnalysisData] = None
    moq_analysis: Optional[MOQAnalysisData] = None
    pallet_analysis: Optional[PalletAnalysisData] = None
    duplicate_detection: Optional[DuplicateDetectionData] = None
    order_comparison: Optional[OrderComparisonData] = None
    backorder_analysis: Optional[BackOrderAnalysisData] = None
    price_analysis: Optional[PriceAnalysisData] = None
    # ADR-034 Phase B — EmailOrderEntryRecipe enrichment.
    email_order_entry_analysis: Optional[EmailOrderEntryAnalysisData] = None
