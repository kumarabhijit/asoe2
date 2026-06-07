"""Request/Response Pydantic models for the ASOE API.

Maps to architecture_v3.md Section 8.2 endpoint contracts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.models import (
    ClassifierType,
    ExecutedNode,
    GatewayCallSpan,
    LifecycleState,
    OrderEvent,
)

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
    # ADR-042 §2.2.6 — operator corrections to the extracted order, applied by
    # SubmitToErpRecipe on a SUBMIT_TO_ERP disposition with a before/after audit.
    # Shape: {"header": {field: value}, "lines": {line_num: {field: value}}}.
    corrections: Optional[Dict[str, Any]] = None
    # ADR-042 Phase 4 — operator reply params for a DRAFT_REPLY disposition,
    # composed by ReplyDraftRecipe. Shape: {"recipient": str, "template_name":
    # str, "edits": {"subject"/"body": str}}.
    reply: Optional[Dict[str, Any]] = None


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


class AutonomyLevelInfo(BaseModel):
    """One autonomy tier for the UI to render/sort (ADR-042 §5/§8).

    `rank` is the degree of automation (higher == more autonomous) under the
    active `autonomy_vocab_version`; the UI orders the ladder by `rank` rather
    than hardcoding a map (asoe-ui Guardrail #1). This is the same vocabulary
    the engine's gating ladders resolve under (ADR-042 §5 migration complete)."""

    level: str
    label: str
    rank: int


class TaxonomyDisplayLabels(BaseModel):
    """Operator-facing display strings for taxonomy codes (ADR-045).

    Projected from ``db/seeds/case_taxonomy.yaml`` through the generated
    constants — the codes remain the single source of truth for control
    flow, these are the *human* strings. The UI is a pure projector: it
    renders these and never invents its own label for a code (Guardrail
    #2). Adding/renaming a supergroup or intent in the YAML surfaces here
    with zero UI code change. Keys are taxonomy codes (``SG_*`` / ``INT_*``).
    """

    supergroups: Dict[str, str] = Field(default_factory=dict)
    intents: Dict[str, str] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    """GET /api/v1/health"""

    status: str = "ok"
    version: str
    kill_switch: bool
    explain_mode: bool
    allowed_intents: List[str]
    lifecycle_states: List[LifecycleState]
    allowed_recipes: List[str]
    allowed_resolution_actions: List[str]
    allowed_override_reason_tags: List[str]
    # Per-intent override-reason vocabulary. Same keys as allowed_intents;
    # values are the subset of allowed_override_reason_tags that apply to
    # each intent. Seeded with the global set for every intent today —
    # product/compliance will curate these in a follow-up. Consumed by
    # the UI Override chooser to narrow its options by record.intent.
    allowed_override_reason_tags_by_intent: Dict[str, List[str]]
    # Phase 28.5.x §D1 (CaseListPane) — the seven case-status values
    # (`contracts/models.py::CaseStatus`). Sourced once via
    # `useHealth.allowed_case_statuses` so the UI's CaseListPane
    # filter chips don't hardcode the enum, preserving Guardrail #1.
    allowed_case_statuses: List[str] = Field(default_factory=list)
    # Requirements §3 glossary — case origin values (CUSTOMER | API). The
    # UI consumes this from health exclusively (Guardrail #1, no
    # hardcoded enum).
    allowed_case_origins: List[str] = Field(default_factory=list)
    # ADR-042 §5/§8 — the autonomy vocabulary the UI renders. The UI sorts the
    # ladder by `rank` and reads labels from here (no hardcoded autonomy map,
    # Guardrail #1). `autonomy_vocab_version` is the version these rows resolve
    # under (records stamp their own version; this is the current display set).
    autonomy_vocab_version: str = ""
    allowed_autonomy_levels: List[AutonomyLevelInfo] = Field(default_factory=list)
    # ADR-045 — operator-facing display strings + the supergroup→intent
    # fan-out map. `display_labels` lets the UI render humanised names for
    # taxonomy codes without a hand-authored label map (which had drifted:
    # MANUAL_ORDER_INTAKE was mislabelled "Email Order Intake" on an EDI
    # order). `intents_by_supergroup` drives the summary qualifier rule —
    # the intent chip is shown only when its supergroup fans out to >1
    # intent, so a 1:1 bucket like SG_NEW_ORDER reads as just "New Order".
    display_labels: TaxonomyDisplayLabels = Field(default_factory=TaxonomyDisplayLabels)
    intents_by_supergroup: Dict[str, List[str]] = Field(default_factory=dict)


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
    lifecycle_state: LifecycleState
    shadow_verdict: Optional[str] = None
    selected_recipe: Optional[str] = None
    final_status: Optional[str] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    # ADR-038 §H.6 — surfaced on the summary so the UI can deeplink
    # to the parent case from the exception queue without an extra
    # round-trip. None on Tier-1 stateless records and pre-Phase-H.3
    # legacy rows.
    parent_case_id: Optional[str] = None
    # Case & Intent Super-Group surface (requirements §5).
    # Optional in Phase 2; populated by the Phase 3 classifier wiring.
    supergroup_code: Optional[str] = None
    intent_code: Optional[str] = None
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
    lifecycle_state: LifecycleState
    shadow_verdict: Optional[str] = None
    selected_recipe: Optional[str] = None
    final_status: Optional[str] = None
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    trace_id: Optional[str] = None
    # ADR-038 Phase H.3 / S15a — parent case for this record.
    # Populated by `materialise_for_event` at persist time. The UI uses
    # this to compute /cases/{parent}?record=<id> deep-links from a
    # per-record handle (the standalone /exceptions/[id] route was
    # retired alongside the case-centric pivot). None on Tier-1
    # stateless records and pre-Phase-H.3 legacy rows; both are
    # exceptional and the UI throws loudly rather than silently
    # routing to a phantom page.
    parent_case_id: Optional[str] = None
    # Case & Intent Super-Group surface (requirements §5).
    # Optional in Phase 2; populated by the Phase 3 classifier wiring.
    supergroup_code: Optional[str] = None
    intent_code: Optional[str] = None
    divergence_reason: Optional[str] = None
    sap_block_field: Optional[str] = None
    scope: Optional[str] = None
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
    prior_lifecycle_state: LifecycleState
    new_trace_id: str
    new_shadow_verdict: Optional[str] = None
    new_final_status: Optional[str] = None
    new_lifecycle_state: LifecycleState
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
    # Autonomy vocab v2 (ADR-042 §5). SHIP_TO_MISMATCH resolves to L4 (escalate
    # to human) under v2 — the full L1-L4 range is admissible.
    autonomy_level: Literal["L1", "L2", "L3", "L4"]
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


class EmailAttachmentManifestEntry(BaseModel):
    """One row in `EmailSourceData.attachment_manifest`.

    `name` and `mime_type` are sufficient for the operator to
    triage what arrived; `bytes` lets the UI render a size hint.
    `sha256` is the per-attachment tamper-evidence hash (the
    analog of `EmailSourceData.body_hash` for the body) — computed
    over the raw bytes by the attachment store
    (`gateways/attachment_store.py`). `attachment_id` references the
    stored blob so the UI can build a download link
    (GET /cases/{id}/attachments/{attachment_id}). Both are optional:
    they are None until the attachment is persisted to the store
    (preview-only before a producer populates them).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    mime_type: str
    bytes: int = 0
    sha256: Optional[str] = None
    attachment_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Evidence highlighting (ADR-043) — backend-authoritative highlight anchors.
# ---------------------------------------------------------------------------

EvidenceSupportsKind = Literal["extracted_field", "constraint", "decision"]
"""Closed vocabulary of what an anchor supports (ADR-043 §2.2). Parity-locked
across asoe2↔asoe-ui (`tests/architectural/evidence_supports_kind_parity.test.ts`)
so the UI maps it with a `default` fallback — adding a kind is backend-only."""

EvidenceAnchorSource = Literal["text_derived", "spatial_extracted"]
"""Discriminator: Phase-1 anchors are `text_derived` (no geometry); Phase-2
`spatial_extracted` carry verified page/bbox (ADR-045)."""


class MatchKey(BaseModel):
    """Deterministic locate key (ADR-043 §2.4). The backend computes this so the
    viewer does a pure *literal* locate in the rendered text layer — never a
    client-side search (Guardrail #6). `occurrence_index` disambiguates repeated
    tokens (the AMBIGUOUS hazard)."""

    model_config = ConfigDict(extra="forbid")

    normalized_text: str
    occurrence_index: int = 0


class EvidenceAnchor(BaseModel):
    """A backend-authoritative highlight anchor (ADR-043 §2.2).

    The composer projects one per extracted evidence field; the viewer *locates*
    and renders it, inventing nothing. The audit-authoritative identity is
    `audit_tuple()` — `(attachment_id, source_sha256, text, supports_ref)` —
    explicitly decoupled from on-screen position (ADR-043 §2.3): a best-effort
    box is never the audit unit. Phase-1 (`text_derived`) anchors carry no
    geometry; that invariant is enforced by the validator below.
    """

    model_config = ConfigDict(extra="forbid")

    attachment_id: str
    anchor_source: EvidenceAnchorSource
    text: str
    match_key: MatchKey
    supports_kind: EvidenceSupportsKind
    supports_ref: str
    label: str
    source_sha256: str
    """Binds the anchor to the exact attachment bytes it overlays — a re-version
    can never silently re-point an authorised highlight."""

    # Spatial fields — ADR-045. MUST be None when anchor_source == text_derived.
    page: Optional[int] = None
    bbox: Optional[List[float]] = None
    confidence: Optional[float] = None
    rendition_hash: Optional[str] = None
    """Frozen-rendition basis the geometry was computed against (ADR-044 §2.5).
    Spatial anchors only; binds page/bbox to the exact render (raster + dpi +
    renderer_version) so a re-render under a moved basis can be detected. Never
    audit-bearing — like page/bbox, position is best-effort."""

    @model_validator(mode="after")
    def _phase1_carries_no_geometry(self) -> "EvidenceAnchor":
        if self.anchor_source == "text_derived" and (
            self.page is not None
            or self.bbox is not None
            or self.confidence is not None
            or self.rendition_hash is not None
        ):
            raise ValueError(
                "text_derived anchors carry no geometry (ADR-043 §2.2); "
                "spatial coordinates require anchor_source='spatial_extracted' "
                "and the ADR-045 verifier."
            )
        return self

    def audit_tuple(self) -> "tuple[str, str, str, str]":
        """The audit-authoritative identity, decoupled from pixel position."""
        return (self.attachment_id, self.source_sha256, self.text, self.supports_ref)


class AttachmentErasureTombstone(BaseModel):
    """PII-free tombstone for an erased attachment (PARITY-0.5 / ADR-044 §2.4).

    Written into the immutable audit chain (ADR-023) as the ``new_value``
    of the ``ATTACHMENT_ERASED`` event so a regulator can later verify a
    deletion happened against content of a specific hash. NEVER add
    ``content`` or ``name`` fields here — both leak PII. The schema is
    locked in `compliance/audit_bearing_registry.yaml::AttachmentErasureTombstone`
    behind the CODEOWNERS gate.
    """

    model_config = ConfigDict(extra="forbid")

    attachment_id: str
    tenant_id: str
    sha256: str
    """Content hash — survives the bytes so a regulator can prove a
    decision was made against content of this hash."""

    case_id: Optional[str] = None
    size_bytes: int
    mime_type: str
    erased_at: str
    """ISO-8601 UTC timestamp of the erasure."""

    erased_by: str
    """Actor identity — user `sub` for operator-initiated erasures,
    ``system:retention-sweeper`` (or similar) for scheduled retention sweeps."""

    reason: Optional[str] = None
    """Free-text reason captured at erasure time (e.g.
    ``right-to-erasure-request``, ``retention-policy-90d-expired``)."""


class EmailSourceData(BaseModel):
    """EmailOrderEntryRecipe → UI `email_source` (ADR-034 Phase G).

    Email-channel order intake source-of-truth substrate, projected
    by `adapt_email_source` from
    `record.enrichment_context["email_source_context"]` (the
    `email_intake/fetch_message` gateway response).

    Mirrors `asoe-ui/src/types/exceptions.ts::EmailSourceData`. Mounts on
    the Exception Queue detail page above `EmailOrderEntrySection`
    via data-presence dispatch (no per-intent page-level branching —
    CLAUDE.md Guardrail #1).

    Compliance posture (Verdict 2026-04-22):
      * `from_address`, `received_at`, `subject`, `body_hash`,
        `attachment_manifest` are audit-bearing — the operator
        authorising the order needs to see the source-of-truth
        substrate the recipe acted on.
      * `body_excerpt` is contextual — the full body is referenced by
        `body_hash` for tamper-detection and may be redacted from the
        excerpt for PII; absence is a structural omission, not an
        audit gap.
    """

    model_config = ConfigDict(extra="forbid")

    from_address: str
    received_at: str  # ISO-8601
    subject: str
    body_hash: str
    """SHA-256 of the canonicalised email body. Lets the audit trail
    detect post-hoc tampering of the inbound payload without storing
    PII-shaped body content in the registry."""
    attachment_manifest: List[EmailAttachmentManifestEntry] = []
    body_excerpt: Optional[str] = None
    """Optional human-readable excerpt of the email body — typically
    the first 240 chars after canonicalisation. Contextual: absent
    when redaction policy strips PII or when the body is unavailable."""

    source_email_id: Optional[str] = None
    """Optional Inbox correlation id (ADR-034 Phase G). When present,
    the Exception Queue detail page surfaces a "View source email"
    back-link to /inbox?msg=<id>. Contextual: absent when the
    upstream `email-intelligence-agent` integration (Phase F /
    proposed ADR-036) hasn't shipped — the inline source rendering
    on the detail page is the primary surface."""

    evidence_anchors: List[EvidenceAnchor] = Field(default_factory=list)
    """Backend-authoritative highlight anchors (ADR-043) the attachment-preview
    viewer locates and renders. Phase-1 anchors are derived from the extracted
    entities' `source_span`; the viewer never invents evidence (Guardrail #6).
    Empty when no attachment is stored or no locatable evidence exists."""


class ConfidenceSignal(BaseModel):
    """A confidence score plus its calibration provenance (trust surface).

    `value` is the canonical 0.0–1.0 score. `calibrated` states whether
    `value` has been calibrated to an observed-accuracy definition (ECE /
    Brier per ADR-032) — until that loop ships it is False, and the UI must
    frame the number as a raw model score, never a validated probability of
    correctness. `method` names the producer so an auditor can reconstruct
    WHICH scorer produced the number; `sample_n` is the cohort size behind a
    calibrated band (None for a raw single score).

    Honesty rule (Verdict 2026-04-22 / Guardrail #6): the projector restates
    the raw model score with calibrated=False — it never fabricates a
    calibration claim, and it returns None for a missing / non-positive score
    so the surface stays absent rather than showing a synthetic 0 the operator
    can't distinguish from a real low-confidence reading."""

    value: float
    calibrated: bool
    method: Optional[str] = None
    sample_n: Optional[int] = None

    @classmethod
    def from_raw(cls, raw: Any, *, method: str) -> Optional["ConfidenceSignal"]:
        """Project a raw producer score (expected 0.0–1.0) into an
        uncalibrated signal. Returns None when the score is missing or
        non-positive (mirrors the AnalysisResponse.confidence "stay at 0,
        UI hides the bar" rule — no fabricated mid-range default). The value
        is clamped to [0, 1] so a malformed score can never paint an
        over-wide bar."""
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
            return None
        return cls(
            value=max(0.0, min(1.0, float(raw))),
            calibrated=False,
            method=method,
        )


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
    # ADR-032 — the composite score as a typed signal with calibration
    # provenance. Optional + additive (the scalar above is retained,
    # Guardrail #7); None until a real score exists. Projected by the
    # adapter via ConfidenceSignal.from_raw (uncalibrated until the loop ships).
    composite_confidence_signal: Optional[ConfidenceSignal] = None
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
    # Trim-plan roll-ups — audit-bearing. Computed authoritatively here (the
    # composer's typed contract) from `trim_plan` so the displayed totals are a
    # single server-side source of truth, not a client-side `reduce` in the UI
    # projector (UI Guardrail #6 — no audit totals derived in the section
    # component). Any inbound value is overridden by the deterministic sum.
    trimmed_total: float = 0.0
    delta_total: float = 0.0
    # Grandfathered audit-bearing — populated when the SAP contract
    # gateway lands.
    contract_ref: Optional[str] = None
    block_status: Optional[str] = None
    block_reason: Optional[str] = None

    @model_validator(mode="after")
    def _compute_trim_totals(self) -> "OverMaxAnalysisData":
        self.trimmed_total = sum(line.trimmed_to for line in self.trim_plan)
        self.delta_total = sum(line.delta for line in self.trim_plan)
        return self


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
    # ADR-032 — detection confidence as a typed signal (canonical 0-1) with
    # calibration provenance. Additive (scalar retained, Guardrail #7);
    # projected by the adapter from the 0-1 composite_score.
    confidence_signal: Optional[ConfidenceSignal] = None
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


class ExtractedEntity(BaseModel):
    """A single structured value the AI intake agent pulled from the email
    / attachments (ADR-042 Phase 2 — Entities section). `kind` is the
    free-form category (order_id | material | date | po | invoice | qty | …);
    `source_span` is the verbatim text it was extracted from (audit trail of
    where a value came from)."""

    key: str
    value: str
    kind: str
    confidence: Optional[float] = None
    # ADR-032 — per-entity confidence as a typed signal with calibration
    # provenance. Optional + additive (scalar retained, Guardrail #7);
    # projected by compose_entities_analysis via ConfidenceSignal.from_raw.
    confidence_signal: Optional[ConfidenceSignal] = None
    source_span: Optional[str] = None
    # ADR-043 / ADR-045 field↔source linking. The deterministic locate key
    # of the EvidenceAnchor that supports this entity — i.e. the anchor's
    # `supports_ref`. When present, the UI couples this entity row to its
    # in-document highlight (click the entity → the safety bar / overlay
    # highlights the source span). Pure provenance, never a binding control:
    # it is a pass-through from the extraction producer (the gateway emits
    # the same ref it stamped on the matching anchor), NOT synthesised by the
    # composer — keeping the field↔anchor pairing backend-authoritative
    # (Guardrail #6, no UI-side ref derivation). None until the producer
    # populates it.
    evidence_ref: Optional[str] = None


class EntitiesAnalysisData(BaseModel):
    """Entities tab (ADR-042 Phase 2). Contextual evidence — the extracted
    entities the operator reviews; the binding control fields remain
    `recommended_action` / `autonomy_level`. Projected by the composer from
    the intake gateway's extraction; preview-only until that adapter lands."""

    extracted: List[ExtractedEntity] = Field(default_factory=list)


class SapDataAnalysisData(BaseModel):
    """SAP Data tab (ADR-042 Phase 2). Live SAP system-of-record context the
    operator authorises against — which system, its validation state, and the
    order's financial value. Projected by the composer from the SAP gateway
    read; preview-only until that adapter lands. `system` / `validation_status`
    / `order_value_usd` are audit-bearing (the decision applies to a specific
    SAP order in a specific state at a specific value); `sap_doc_number` is a
    contextual reference."""

    system: str
    validation_status: str
    order_value_usd: Optional[float] = None
    sap_doc_number: Optional[str] = None


class OrderEntryHeader(BaseModel):
    """Extracted order-header fields (ADR-042 Phase 3 — Order Entry tab)."""

    customer_po: Optional[str] = None
    order_type: Optional[str] = None
    sales_org: Optional[str] = None
    dist_channel: Optional[str] = None
    ship_window_from: Optional[str] = None
    ship_window_to: Optional[str] = None
    requested_date: Optional[str] = None


class OrderEntryLineItem(BaseModel):
    """One extracted order line. `mdm_matched` = matched against material
    master; None when the master lookup hasn't run."""

    line_num: str
    material: str
    description: Optional[str] = None
    quantity: float
    uom: Optional[str] = None
    unit_price: Optional[float] = None
    mdm_matched: Optional[bool] = None
    # ADR-032 — per-line extraction confidence so the operator sees WHICH line
    # the model was unsure about (the section-level score alone hides that).
    # Additive + optional (Guardrail #7); `confidence_signal` carries the
    # calibration provenance, projected by the composer via from_raw.
    confidence: Optional[float] = None
    confidence_signal: Optional[ConfidenceSignal] = None


class OrderEntryValidationFlag(BaseModel):
    """A validation finding on the extracted order. `severity` ∈
    ERROR | WARNING | INFO."""

    field: str
    severity: str
    message: str


class OrderEntryExtraction(BaseModel):
    """Order Entry tab (ADR-042 Phase 3) — the structured order the AI intake
    agent extracted from the email/attachments, for operator review before ERP
    submit. Projected by the composer from the extraction-gateway read;
    preview-only until that gateway lands. The agent's recommendation /
    autonomy live separately on `email_order_entry_analysis`."""

    source_type: str
    confidence: float
    # ADR-032 — extraction confidence as a typed signal with calibration
    # provenance. Optional + additive (scalar retained, Guardrail #7);
    # projected by compose_order_entry_extraction via ConfidenceSignal.from_raw.
    confidence_signal: Optional[ConfidenceSignal] = None
    header: OrderEntryHeader
    customer_name: Optional[str] = None
    customer_bp: Optional[str] = None
    line_items: List[OrderEntryLineItem] = Field(default_factory=list)
    validation_flags: List[OrderEntryValidationFlag] = Field(default_factory=list)


class Edi850Segment(BaseModel):
    """One ANSI X12 segment of the built 850. `seg_id` is the segment tag
    (ISA/BEG/PO1/…); `elements` are its ordered data elements (excluding the
    tag); `raw` is the assembled X12 line (element-separated, segment-
    terminated); `meaning` is the human-readable decode (Segment Map view);
    `group` buckets the segment for the viewer's colour legend ∈
    envelope | header | dates | party | line | trailer."""

    seg_id: str
    elements: List[str] = Field(default_factory=list)
    raw: str
    meaning: str
    group: str


class Edi850Envelope(BaseModel):
    """ISA/GS/ST interchange + functional-group identifiers (Decoded view's
    Interchange Envelope card)."""

    sender_id: str
    receiver_id: str
    interchange_control_number: str
    group_control_number: str
    transaction_set_control_number: str
    usage_indicator: str
    x12_version: str


class Edi850Header(BaseModel):
    """BEG/CUR/DTM purchase-order header (Decoded view's PO Header card).
    `purpose_code` is BEG01 (e.g. ``00`` = original); `po_type` is BEG02
    (e.g. ``SA`` = stand-alone)."""

    purpose_code: str
    po_type: str
    po_number: str
    po_date: Optional[str] = None
    currency: Optional[str] = None
    requested_delivery_date: Optional[str] = None


class Edi850Party(BaseModel):
    """An N1/N3/N4 party loop (Decoded view's Party Information card).
    `entity_code` is the N101 qualifier (``BY`` buyer / ``SE`` seller /
    ``ST`` ship-to); `role` is its decoded label."""

    entity_code: str
    role: str
    name: str
    id_qualifier: Optional[str] = None
    id_value: Optional[str] = None
    address: Optional[str] = None
    city_state_zip: Optional[str] = None


class Edi850LineItem(BaseModel):
    """A PO1 baseline-item line (Decoded view's Line Items table).
    `product_qualifier` is the PO1 product/service-ID qualifier (e.g. ``VP``
    vendor part / ``IN`` buyer item); `extended_amount` = quantity ×
    unit_price (deterministic)."""

    line_num: str
    quantity: float
    uom: str
    unit_price: Optional[float] = None
    product_qualifier: Optional[str] = None
    product_id: Optional[str] = None
    description: Optional[str] = None
    extended_amount: Optional[float] = None


class Edi850Totals(BaseModel):
    """CTT transaction totals. `total_line_items` = CTT01 (PO1 count);
    `total_quantity` = CTT02 (hash total of quantities); `total_amount` =
    Σ extended_amount (deterministic, may be None when no line carries a
    price)."""

    total_line_items: int
    total_quantity: float
    total_amount: Optional[float] = None


class Edi850Document(BaseModel):
    """EDI 850 Audit tab (ADR-042 Phase 5). The ANSI X12 5010 purchase-order
    transaction set the system reconstructs from the reviewed order, built
    deterministically by the `edi_850` builder (`gateways/edi850.py`) — a pure,
    fully unit-testable port of the prototype's client-side `buildEDI850`. It is
    the audit evidence an operator inspects before the order is transmitted to
    the seller's ERP. Projected by the composer from the builder's
    `enrichment_context["edi_850"]` read; preview-only until that producer is
    wired. The three prototype sub-views map onto this contract: Decoded
    (`envelope` / `header` / `parties` / `line_items` / `totals`), Raw X12
    (`raw_x12`), and Segment Map (`segments`)."""

    standard: str = "ANSI X12 5010"
    transaction_set: str = "850"
    envelope: Edi850Envelope
    header: Edi850Header
    parties: List[Edi850Party] = Field(default_factory=list)
    line_items: List[Edi850LineItem] = Field(default_factory=list)
    totals: Edi850Totals
    segments: List[Edi850Segment] = Field(default_factory=list)
    raw_x12: str


class ConstraintCheck(BaseModel):
    """One deterministic constraint evaluation in the Change Analysis (ADR-042
    Phase 6). `status` ∈ PASS | CONDITIONAL | WARNING. `agent` is a cosmetic
    label (the evaluating discipline) — NOT audit-bearing (ADR-042 §6: agent
    timings/labels are decorative). `system_ref` names the system of record the
    check reasons over (e.g. ``SAP MM/ATP``)."""

    name: str
    status: str
    detail: str
    metric: Optional[str] = None
    agent: Optional[str] = None
    system_ref: Optional[str] = None


class ChangeItem(BaseModel):
    """One field of the requested order change (the from/to grid)."""

    field: str
    from_value: Optional[str] = None
    to_value: Optional[str] = None


class ConstraintEvaluation(BaseModel):
    """The variable-cardinality constraint evaluation for a requested order
    change (ADR-042 Phase 6). `checks` is an N-length list — only the
    constraints whose backing signal is present are evaluated (no fixed 10).
    `lifecycle_stages` is the ordered stage vocabulary (the UI renders the bar
    from the payload — no hardcoded stage list client-side); `lifecycle_index`
    is the current stage's position within it."""

    lifecycle_stages: List[str] = Field(default_factory=list)
    lifecycle_index: Optional[int] = None
    change_items: List[ChangeItem] = Field(default_factory=list)
    checks: List[ConstraintCheck] = Field(default_factory=list)
    pass_count: int = 0
    conditional_count: int = 0
    warning_count: int = 0


class ScenarioOption(BaseModel):
    """One resolution scenario the evaluator surfaces for a change (ADR-042
    Phase 6). Variable cardinality — M scenarios, not the prototype's fixed 7.
    `recommended` marks the evaluator's preferred option; `financial_delta_usd`
    is the deterministic revenue delta vs the as-requested change."""

    name: str
    description: str
    recommended: bool = False
    impact: Optional[str] = None
    financial_delta_usd: Optional[float] = None


class ChangeDecision(BaseModel):
    """The Change Analysis decision panel (ADR-042 Phase 6). `requires_cosign`
    is set deterministically when `revenue_impact_usd` meets the ADR-040
    four-eyes threshold (`contracts/policy.HIGH_VALUE_OVERRIDE_THRESHOLD_USD`) —
    the recipe surfaces the gate; actioning the change is a separate
    Shadow-gated disposition. `confidence` ∈ [0, 1]."""

    recommended_action: str
    confidence: float
    rationale: Optional[str] = None
    revenue_impact_usd: Optional[float] = None
    requires_cosign: bool = False
    sap_actions: List[str] = Field(default_factory=list)


class ChangeAnalysis(BaseModel):
    """Change Analysis tab (ADR-042 Phase 6) — the deterministic evaluation of a
    requested order change: an N-constraint evaluation, M resolution scenarios,
    and a decision. Built by the recipe-homed evaluator
    (`recipes/ChangeAnalysisRecipe.evaluate_change`, pure + deterministic;
    thresholds from `contracts/policy.py`, NOT `constraints/`). Projected by the
    composer from `enrichment_context["change_analysis"]`; preview-only until
    that producer is wired (Guardrail #6 — no partial truth)."""

    evaluation: ConstraintEvaluation
    scenarios: List[ScenarioOption] = Field(default_factory=list)
    decision: ChangeDecision


class KnowledgeGraphNode(BaseModel):
    """One node in the Change/Knowledge Graph (ADR-042 Phase 7). `kind` buckets
    the node for the viewer's legend (e.g. ``order`` / ``customer`` /
    ``material`` / ``sap_doc`` / ``entity``); `detail` is an optional one-line
    annotation."""

    id: str
    label: str
    kind: str
    detail: Optional[str] = None


class KnowledgeGraphEdge(BaseModel):
    """A directed relationship between two `KnowledgeGraphNode` ids (ADR-042
    Phase 7). `relation` is the decoded edge label (e.g. ``places`` /
    ``contains`` / ``validated_by``)."""

    source: str
    target: str
    relation: str


class KnowledgeGraphPayload(BaseModel):
    """Knowledge Graph tab (ADR-042 Phase 7) — a net-new DERIVED projection over
    the case's already-resolved entities (the order, its customer, materials,
    SAP doc, and extracted entities). Built deterministically by
    `gateways/knowledge_graph.build_knowledge_graph` (pure) — there is no
    standalone KG data source today, so this is a projection of existing
    enrichment context, not invented data (ADR-042 §5b — deferrable behind
    demand). Projected by the composer from
    `enrichment_context["knowledge_graph"]`; preview-only until that producer is
    wired. `root_id` is the focal node (the order/case)."""

    nodes: List[KnowledgeGraphNode] = Field(default_factory=list)
    edges: List[KnowledgeGraphEdge] = Field(default_factory=list)
    root_id: Optional[str] = None


class DraftReplyEdit(BaseModel):
    """One operator edit applied to a generated reply (before/after audit)."""

    field: str
    before: Optional[str] = None
    after: Optional[str] = None


class DraftReplyRevision(BaseModel):
    """One entry in the versioned history of a reply draft (operator edit +
    versioned history). The chain is append-only: version 1 is the AI-generated
    draft; each operator edit appends a new revision. The last element is always
    the current draft (mirrored onto the top-level `DraftReply` fields for
    back-compat). `source` ∈ AI_GENERATED | OPERATOR_EDIT records WHO authored
    the revision — the SOX who/what/when of a financially-relevant buyer reply."""

    version: int  # 1-based, monotonic; last element is the current draft
    subject: Optional[str] = None
    body: Optional[str] = None
    # Field-level before/after for THIS revision (vs. the previous revision).
    edits_applied: List[DraftReplyEdit] = Field(default_factory=list)
    author: str  # AI service id for the generated draft, or operator sub for an edit
    authored_at: str  # ISO-8601
    source: str  # "AI_GENERATED" | "OPERATOR_EDIT"


class DraftReply(BaseModel):
    """AI Draft Reply evidence (ADR-042 Phase 4 contract; surfaced on the
    analysis in Phase 7). The current generated reply for the case, projected by
    the composer from `resolution_data["reply_draft"]` (the ReplyDraftRecipe
    output a DRAFT_REPLY disposition persisted). `status` ∈ DRAFTED | REJECTED;
    a REJECTED draft carries a `reason` and no body. Read-only evidence — sending
    is a separate Shadow-gated disposition (SEND_REPLY).

    `revisions` is the append-only version history (operator edit + versioned
    history); the top-level `subject`/`body`/`edits_applied` always mirror the
    LATEST revision so existing readers keep working."""

    status: str
    reason: Optional[str] = None
    template_name: Optional[str] = None
    recipient: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    edits_applied: List[DraftReplyEdit] = Field(default_factory=list)
    drafted_by: Optional[str] = None
    drafted_at: Optional[str] = None
    revisions: List[DraftReplyRevision] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Review-Quality console (Phase 4) — typed response for
# GET /api/v1/metrics/reviewer-activity. Typed (not a bare dict) so the
# contract is enforced across local/Vercel (mock) and Azure (real): the UI
# generates its type from this schema, so the autonomy console cannot drift
# between the mock payload and the live endpoint.
# ---------------------------------------------------------------------------
class ReviewerActivityCohort(BaseModel):
    decisions: int
    layer2_open_rate: float


class ReviewerActivityDwellBucket(BaseModel):
    """Cumulative histogram bucket. `le_seconds` is the upper edge; None is the
    +Inf overflow bucket."""

    le_seconds: Optional[float] = None
    count: int


class ReviewerActivityByHighlight(BaseModel):
    shown: ReviewerActivityCohort
    not_shown: ReviewerActivityCohort


class ReviewerActivitySnapshot(BaseModel):
    """Automation-bias review-scrutiny SLIs (the "reviewing vs rubber-stamping"
    signal). Process-local since restart; the fleet view is in Grafana."""

    scope: str
    decisions: int
    layer2_opened: int
    layer2_open_rate: float
    dwell_seconds_histogram: List[ReviewerActivityDwellBucket] = Field(default_factory=list)
    dwell_seconds_sum: float
    by_highlight: ReviewerActivityByHighlight


class AnalysisResponse(BaseModel):
    """GET /api/v1/exceptions/{id}/analysis"""

    diagnosis: str
    confidence: int
    # ADR-032 trust surface — the same confidence as a typed signal carrying
    # its calibration provenance. Optional + None when no real classifier
    # confidence is available (mirrors `confidence == 0`), so the UI frames
    # the score honestly (raw model score vs calibrated probability) without a
    # fabricated default. Projected from the classifier score in the read path
    # via `ConfidenceSignal.from_raw`; the scalar `confidence` is retained for
    # back-compat (Guardrail #7 — additive, not a replacement).
    confidence_signal: Optional[ConfidenceSignal] = None
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
    # ADR-034 Phase G — email source-of-truth substrate (PO-driven IA
    # correction). SECONDARY adapter on EmailOrderEntryRecipe.py;
    # mounts on the Exception Queue detail page above
    # email_order_entry_analysis via data-presence dispatch.
    email_source: Optional[EmailSourceData] = None
    # ADR-042 Phase 2 — Customer Inbox Entities tab. Preview-only until the
    # intake-extraction composer adapter lands; the rich type is a product
    # commitment (Guardrail #7).
    entities_analysis: Optional[EntitiesAnalysisData] = None
    # ADR-042 Phase 2 — Customer Inbox SAP Data tab. Preview-only until the
    # SAP-gateway composer adapter lands.
    sap_data_analysis: Optional[SapDataAnalysisData] = None
    # ADR-042 Phase 3 — Order Entry tab (extracted order form). Preview-only
    # until the extraction-gateway composer adapter lands.
    order_entry_extraction: Optional[OrderEntryExtraction] = None
    # ADR-042 Phase 5 — EDI 850 Audit tab (deterministic X12 850 reconstruction).
    # Preview-only until the edi_850 builder producer populates enrichment_context.
    edi_850_audit: Optional[Edi850Document] = None
    # ADR-042 Phase 6 — Change Analysis tab (deterministic constraint evaluation
    # + scenarios + decision). Preview-only until the change_analysis producer
    # populates enrichment_context.
    change_analysis: Optional[ChangeAnalysis] = None
    # ADR-042 Phase 7 — Knowledge Graph tab (derived entity projection).
    # Preview-only until the knowledge_graph producer populates enrichment_context.
    knowledge_graph: Optional[KnowledgeGraphPayload] = None
    # ADR-042 Phase 7 — AI Draft Reply evidence (projected from
    # resolution_data["reply_draft"]). Absent until a DRAFT_REPLY disposition runs.
    draft_reply: Optional[DraftReply] = None

    # ── Presentation hint: the primary comparison section ────────────
    # The AnalysisResponse FIELD NAME of this record's primary
    # enrichment projection (e.g. "price_hold_analysis"). It is the
    # recipe-authoritative comparison the operator should see first —
    # the read path sets it to the field the primary ANALYSIS_ADAPTER
    # lands on when (and only when) that projection cleared audit
    # coverage. None for records with no primary adapter projection
    # (e.g. an auto-resolved EDI order with no discrepancy delta).
    #
    # This is a PRESENTATION hint, not a control field: it carries no
    # business/compliance semantics and never gates routing. The UI
    # uses it to auto-expand the matching `*Section` (Priority-2
    # comparison delta) instead of leaving every enrichment section
    # collapsed. Derived deterministically from the resolved adapter
    # key — no heuristic ranking, so it stays inside the Guardrail-#1
    # "no hardcoded enum dispatch" envelope on the UI side.
    primary_section: Optional[str] = None


# ---------------------------------------------------------------------------
# OrderCase responses — ADR-038 Phase H.6 (case-centric surface)
# ---------------------------------------------------------------------------
#
# The `OrderCase` Pydantic model in `contracts/models.py` is the wire
# shape; it mirrors `asoe-ui/src/types/cases.ts::OrderCase` 1:1, so the
# list response simply echoes it. Filters are intentionally narrow
# (source / status); pagination is a flat top-N for now since case
# volumes are bounded by the materialisation policy (ADR-038 §7.1).


class CaseSummaryDollarImpact(BaseModel):
    """ADR-041 P3e §3.1 — financial-impact projection on
    `CaseListItem`. Mirror of
    `asoe-ui/src/lib/api.ts::CaseSummaryDollarImpact`.

    `amount_cents` is the integer-cent value (avoids float drift
    across JSON); `currency` is ISO 4217 (3-letter, uppercase).
    Both fields are required — a bare amount across multi-currency
    queues is a Compliance Guardrail #6 partial-truth violation.
    """

    model_config = ConfigDict(extra="forbid")

    amount_cents: int
    currency: str = Field(..., min_length=3, max_length=3)


class CaseListItem(BaseModel):
    """GET /api/v1/cases — single row in the list response.

    ADR-041 P3e §3.1 — adds the seven `CaseSummary` projection
    fields to the OrderCase wire shape. Backend serialiser populates
    via `api.case_summary.compute_case_summary()`; UI mirror in
    `asoe-ui/src/lib/api.ts::CaseListItem`.

    The `Dict[str, Any]` permissive shape on this model (rather
    than a strict OrderCase + CaseSummary merge) reflects the
    historical wire shape: the route serialiser `_serialise_case`
    composes `case.model_dump()` + `child_intents` + the new
    summary fields as a plain dict. This model documents the
    expected contract and provides a typed surface for the
    `CaseListResponse.items` list, but tolerates additive
    OrderCase fields without requiring a schema bump on every
    OrderCase change.

    Audit-bearing fields (all but `customer_name`) flow through
    `<EvidenceBlock>` on the UI side (Guardrail #6 — no `?? '—'`
    fallbacks). `dollar_impact` is RBAC-stripped at the route
    boundary for callers without `exceptions:approve` OR
    `exceptions:override`; UI handles the absent value as
    Structurally Omitted.
    """

    model_config = ConfigDict(extra="allow")

    # Inherited OrderCase fields (the route serialiser dumps the
    # full case model; we declare the bare minimum that downstream
    # consumers reference, and let `extra="allow"` carry the rest).
    case_id: str
    tenant_id: str
    origin: str
    source_channel: str
    opened_at: str
    status: str

    # Phase 28.5.x §D2 — derived intent set across child records.
    child_intents: List[str] = Field(default_factory=list)

    # ADR-041 P3e §3.1 — the seven CaseSummary projection fields.
    # All nullable; UI's EvidenceBlock collapses absent cells.
    customer_name: Optional[str] = None
    top_line_sku_code: Optional[str] = None
    top_line_sku_title: Optional[str] = None
    problem_one_liner: Optional[str] = None
    intent: Optional[str] = None
    dollar_impact: Optional[CaseSummaryDollarImpact] = None
    audit_verdict_color: Optional[Literal["R", "A", "G"]] = None


class CaseListResponse(BaseModel):
    """GET /api/v1/cases — list response.

    Shape matches `asoe-ui/src/lib/api.ts::casesApi.list`'s declared
    return type. Items are serialised from `contracts.models.OrderCase`.

    Cursor pagination (added 2026-05-11 via ADR-038 §D7 amendment):
    `cursor` is the opaque page-anchor token for the next page, set
    only when `has_more` is True. Clients loop `do { fetch } while
    (cursor)` to accumulate every page. The cursor is the `case_id`
    of the last item on the current page; the server's stable sort
    (opened_at DESC, case_id DESC tiebreak) means re-fetches with
    the same cursor are deterministic.
    """

    items: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    cursor: Optional[str] = None
    has_more: bool = False


class CaseRecordsResponse(BaseModel):
    """GET /api/v1/cases/{id}/records — attached-record loader response
    (Phase 28.5.x §28.5 follow-up).

    `items` is the list of `ExceptionDetail`-shaped child records
    attached to the case (`ChildCase.parent_case_id == case_id`).
    The UI's CaseDetailPanel uses this both to render the stack of
    per-event sections and to aggregate `aggregated_policy_hits` into
    the L1/L2 PolicyHitBadge surface (ADR-039 §4.5).

    Why a separate field for the aggregate (rather than re-deriving it
    client-side):

      * Authoritative: the backend already has the deduped union with
        the LLM_SHADOW: prefix preserved; making the client recompute
        it from `items[*].shadow_policy_hits` invites drift.
      * Empty-set distinction: the UI hides the section when the
        aggregate is empty (Guardrail #6 — no synthesised "no hits"
        placeholder); we want one place that decides "no hits".
      * Telemetry surface: a future SLI on case-level disagreement
        rate reads off the same aggregate without re-iterating
        children.
    """

    items: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = 0
    aggregated_policy_hits: List[str] = Field(
        default_factory=list,
        description=(
            "Deduped union of `shadow_policy_hits` across all attached "
            "records. L1 rule names (bare strings) and L2 LLM-derived "
            "concerns (LLM_SHADOW: prefix) are preserved; the UI's "
            "PolicyHitBadge distinguishes the two visually."
        ),
    )


class ClassificationHistoryEntry(BaseModel):
    """One row of the case's classification audit trail.

    Mirrors ``contracts.models.ClassificationEvent`` and the
    ``case_classification_history`` table (V020). Requirements §8.6 —
    surfaces the append-only audit for UI / steward reporting.

    Field-set parity with ``ClassificationEvent`` is locked by
    ``tests/test_classification_history_shape.py``: an event field
    that this model doesn't mirror would silently drop data on the
    wire, so the field sets must remain identical.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    case_id: str
    child_case_id: Optional[str] = None
    supergroup_code: str
    intent_code: Optional[str] = None
    classified_at: str
    classified_by: str
    classifier_type: ClassifierType  # Literal["HUMAN","MODEL","RULE"] from contracts
    model_version: Optional[str] = None
    reason_text: Optional[str] = None
    source_event_id: Optional[str] = None
    taxonomy_version: str

    def redact_for_partner(self) -> "ClassificationHistoryEntry":
        """Return a copy with internal-only fields blanked for the
        partner (external retailer) audience.

        Redacts four fields whose values may carry internal info:
          * ``reason_text`` — operator-authored free text (commercial
            notes, escalation flags).
          * ``classified_by`` — replaced with a coarse role token
            (``internal:human``, ``internal:model``, ``internal:rule``)
            so the partner sees who-shape but not internal user IDs /
            email addresses.
          * ``model_version`` — internal model build identifier.
          * ``source_event_id`` — internal trace/event UUID or, for
            API-origin cases, the SAP doc number (reveals SAP doc
            numbering / volume).

        The structural audit (case_id, supergroup_code, intent_code,
        classified_at, classifier_type, taxonomy_version) is preserved
        so the partner can still verify the audit trail's shape.
        ``child_case_id`` is left as-is — it's an opaque UUID, and
        omitting it would also remove the parent-vs-child marker the
        partner audit relies on.
        """
        coarse_who = f"internal:{self.classifier_type.lower()}"
        return self.model_copy(update={
            "reason_text": None,
            "classified_by": coarse_who,
            "model_version": None,
            "source_event_id": None,
        })


class ClassificationHistoryResponse(BaseModel):
    """GET /api/v1/cases/{case_id}/classification-history.

    Returns the case's full audit trail in append order (oldest first).
    ``total`` is the row count; callers paginate client-side today (the
    expected depth is single-digit for the audit window).
    """

    items: List[ClassificationHistoryEntry] = Field(default_factory=list)
    total: int = 0
