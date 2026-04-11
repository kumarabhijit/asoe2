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


# Single source of truth: maps TerminalStatus to exception lifecycle state.
# Consumed by api/store.py (in-memory) and db/repository.py (database).
STATUS_TO_LIFECYCLE: Dict[str, str] = {
    "COMPLETE": "RESOLVED",
    "FAIL_TO_HUMAN": "FAILED",
    "MANUAL_REVIEW_REQUIRED": "PENDING_REVIEW",
    "BLOCKED": "BLOCKED",
    "REJECTED": "REJECTED",
    "COMPLETE_WITH_CHILDREN": "RESOLVED",
}

# 11-state exception lifecycle (architecture_v3.md §9.1).
# Consumed by api/routes/health.py and stats queries.
LIFECYCLE_STATES: List[str] = [
    "INGESTED", "CLASSIFYING", "AUDITING", "PENDING_REVIEW", "ESCALATED",
    "EXECUTING", "RESOLVED", "FAILED", "BLOCKED", "REJECTED", "CLOSED",
]


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
