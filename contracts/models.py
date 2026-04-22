from __future__ import annotations

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


class ComplianceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ShadowStatus
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    reasons: List[str] = Field(default_factory=list)
    policy_hits: List[str] = Field(default_factory=list)
    constrained_by: Optional[str] = None


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
    ``GraphState.resolved_data``.  Recipes never call gateways directly.
    """

    model_config = ConfigDict(extra="forbid")
    gateway_name: str
    operation: str
    params_from_state: Dict[str, str] = Field(
        default_factory=dict,
        description="Maps gateway param name → dot-path into GraphState (e.g. 'event.order_id')",
    )
    result_key: str


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


class GraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: OrderEvent
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

    @model_validator(mode="after")
    def _explanation_for_terminal_state(self) -> "GraphState":
        if self.final_status and not self.explanation:
            self.explanation = f"Workflow terminated with status: {self.final_status.value}"
        return self
