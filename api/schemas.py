"""Request/Response Pydantic models for the ASOE API.

Maps to architecture_v3.md Section 8.2 endpoint contracts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from contracts.models import OrderEvent


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ResolveRequest(OrderEvent):
    """POST /api/v1/exceptions/resolve — same fields as OrderEvent."""

    pass


class OverrideRequest(BaseModel):
    """PATCH /api/v1/exceptions/{id}/override — human override."""

    action: str
    notes: Optional[str] = None
    resolved_by: str


class ApproveRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/approve — resume paused exception."""

    notes: Optional[str] = None


class RejectRequest(BaseModel):
    """POST /api/v1/exceptions/{id}/reject — reject paused exception."""

    reason: Optional[str] = None


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
    trace_id: Optional[str] = None
    resolution_data: Dict[str, Any] = Field(default_factory=dict)
    resolved_by: Optional[str] = None
    resolved_action: Optional[str] = None
    resolution_notes: Optional[str] = None
    created_at: str
    updated_at: str


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


class StatsResponse(BaseModel):
    """GET /api/v1/exceptions/stats — dashboard metrics."""

    total: int = 0
    open: int = 0
    auto_resolved: int = 0
    manual_review: int = 0
    blocked: int = 0
    failed: int = 0


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
    """GET /api/auth/me — current user."""

    sub: str
    email: str
    name: str
    roles: List[str]
    org: str
    permissions: List[str]


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
