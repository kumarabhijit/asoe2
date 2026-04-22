"""Request/Response Pydantic models for the ASOE API.

Maps to architecture_v3.md Section 8.2 endpoint contracts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from contracts.models import OrderEvent


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


class AnalysisResponse(BaseModel):
    """GET /api/v1/exceptions/{id}/analysis"""

    diagnosis: str
    confidence: int
    risk: str
    resolution: str
    lines: List[LineAnalysis] = Field(default_factory=list)

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
