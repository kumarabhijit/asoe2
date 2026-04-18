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


class OverrideRequest(BaseModel):
    """PATCH /api/v1/exceptions/{id}/override — human override of agent recommendation.

    Option A (Phase 1 unified action model): available on PENDING_REVIEW,
    ESCALATED, RESOLVED (GREEN), and BLOCKED (RED) exceptions. Analysts
    cannot Override — this is a manager+ action. Action must be a valid
    AllowedResolutionAction. Notes are mandatory (SOX).

    The auditor identity is derived server-side from the authenticated
    user (user.sub) — clients must not pass a ``resolved_by`` field
    (identity-spoofing risk). Any such extra field is rejected (422).
    """

    model_config = ConfigDict(extra="forbid")

    action: str  # validated against AllowedResolutionAction in the endpoint
    notes: str  # mandatory for SOX audit trail


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


class ApproveRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/approve — resume paused exception."""

    notes: Optional[str] = None


class RejectRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/reject — reject paused exception."""

    reason: Optional[str] = None


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


class AnalysisResponse(BaseModel):
    """GET /api/v1/exceptions/{id}/analysis"""

    diagnosis: str
    confidence: int
    risk: str
    resolution: str
    lines: List[LineAnalysis] = Field(default_factory=list)
