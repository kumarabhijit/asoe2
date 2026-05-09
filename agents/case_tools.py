"""ADR-038 §6.4 — the Case Agent's tool surface.

18 tools grouped by purpose. Each tool is a typed callable with a
structured input schema and a structured output schema. The agent
reads the docstring + signature; that's its effective behavioural spec.

This module defines the **registry shape** and a few representative
tool implementations. Tools that wrap existing L1 deterministic
recipes (validate_*, check_*) call those recipes directly. Tools
that wrap L2 LLM primitives (extract_attachment, draft_buyer_email)
delegate to those primitives. Tools that read case state read from
the case_store + ExceptionStore + per-case event log.

Phase H.5 ships the registry skeleton + a representative subset
(~12 of 18 tools wired). Remaining tools land as their backing
primitives become available; the registry shape is what the L3
Case Agent depends on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Tool call / result envelopes
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """The agent's tool invocation request."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Structured tool output. Errors are returned as observations
    per ADR-038 §6.3 — failures DO NOT raise out of the loop."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    status: str  # "ok" | "error" | "not_found" | "timeout" | "unauthorised"
    data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    latency_ms: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Tool spec + registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Static description of a tool the agent can call.

    The ``description`` is what the LLM sees in the tool surface;
    the docstring summary doubles as the model-facing description
    so a single edit propagates to both reviewers and the agent's
    prompt.
    """

    name: str
    description: str
    handler: Callable[["ToolContext", Dict[str, Any]], ToolResult]
    cost_estimate_usd: float = 0.0
    """Best-estimate per-call cost. The harness deducts this from the
    case budget on each invocation. Tools that wrap LLM primitives
    return their actual cost in ``ToolResult.cost_usd`` and the
    harness reconciles."""


class ToolRegistry:
    """Map of tool name → spec. The registry is built once at
    process start; the harness passes it to the agent loop."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def to_descriptors(self) -> List[Dict[str, str]]:
        """Surface the agent sees as input to the LLM call."""
        return [
            {"name": s.name, "description": s.description}
            for s in self._tools.values()
        ]


# ---------------------------------------------------------------------------
# Tool execution context (agent ↔ tool boundary)
# ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    """What every tool sees: the case it's working on, the tenant id,
    and any per-call extras (e.g. the L2 multimodal provider for
    extract_attachment).

    Tools NEVER reach into the agent's working memory or the LLM
    state directly — they read from this context only. This is how
    we keep tool implementations testable in isolation.
    """

    tenant_id: str
    case_id: str
    extras: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helper: invoke a tool with timing / error coercion
# ---------------------------------------------------------------------------


def invoke_tool(
    registry: ToolRegistry,
    ctx: ToolContext,
    call: ToolCall,
) -> ToolResult:
    """Dispatch a tool call. Coerces exceptions into structured
    observations so the agent loop never crashes on tool failures
    (ADR-038 §6.3 invariant)."""
    spec = registry.get(call.tool_name)
    started = time.monotonic()
    if spec is None:
        return ToolResult(
            tool_name=call.tool_name,
            status="not_found",
            error=f"Tool not registered: {call.tool_name}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    try:
        result = spec.handler(ctx, call.arguments)
    except Exception as exc:  # noqa: BLE001 — defensive boundary
        result = ToolResult(
            tool_name=call.tool_name,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    # Preserve the handler's latency / cost when set; otherwise fill in.
    if result.latency_ms == 0:
        result.latency_ms = elapsed_ms
    return result


# ---------------------------------------------------------------------------
# Representative tool implementations
# ---------------------------------------------------------------------------
#
# These are the tools that don't require live LLM / network calls.
# Tests register them against a ToolRegistry and exercise the agent
# loop end-to-end. Production wires the remaining six tools that
# need LLM primitives (extract_attachment, draft_buyer_email,
# load_example) once the corresponding bundles + providers are
# configured.


def _tool_read_case_summary(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Return a compacted overview of the case (~200 tokens)."""
    from api.store import case_store
    case = case_store.get(ctx.case_id)
    if case is None:
        return ToolResult(
            tool_name="read_case_summary",
            status="not_found",
            error=f"Unknown case_id: {ctx.case_id}",
        )
    return ToolResult(
        tool_name="read_case_summary",
        status="ok",
        data={
            "case_id": case.case_id,
            "tenant_id": case.tenant_id,
            "source": case.source,
            "source_channel": case.source_channel,
            "customer_id": case.customer_id,
            "customer_po_number": case.customer_po_number,
            "sales_order_id": case.sales_order_id,
            "status": case.status,
            "tier": case.tier,
            "opened_at": case.opened_at,
            "sla_deadline": case.sla_deadline,
        },
    )


def _tool_read_extracted_fields(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Read previously-extracted fields for an attachment, served
    from the H.4 cache. The agent calls this *before* extract_attachment
    when it suspects a prior call already extracted the same template.
    """
    from agents.primitives.extract_attachment import extraction_cache
    fingerprint = args.get("template_fingerprint")
    if not isinstance(fingerprint, str):
        return ToolResult(
            tool_name="read_extracted_fields",
            status="error",
            error="missing template_fingerprint",
        )
    cached = extraction_cache.get(ctx.tenant_id, fingerprint)
    if cached is None:
        return ToolResult(
            tool_name="read_extracted_fields",
            status="not_found",
            data={"template_fingerprint": fingerprint},
        )
    return ToolResult(
        tool_name="read_extracted_fields",
        status="ok",
        data={
            "template_fingerprint": fingerprint,
            "fields": [f.model_dump() for f in cached.fields],
            "format": cached.format,
        },
    )


def _tool_write_case_note(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Persist an agent note onto the case event log. Used for
    future-self memory ('I tried this last time and it didn't work')
    and audit-bearing rationale."""
    note = args.get("note")
    if not isinstance(note, str) or not note.strip():
        return ToolResult(
            tool_name="write_case_note",
            status="error",
            error="missing or empty `note`",
        )
    audit_visible = bool(args.get("audit_visible", True))
    # Persist via the per-case event log (lives on the case store).
    # Phase H.5 keeps the event log on the case as a list of dicts;
    # Phase H.7 promotes it to a typed CaseEvent table.
    from api.store import case_store
    case = case_store.get(ctx.case_id)
    if case is None:
        return ToolResult(
            tool_name="write_case_note",
            status="not_found",
            error=f"Unknown case_id: {ctx.case_id}",
        )
    # Tag the note in working_memory_summary so subsequent agent turns
    # surface it on read_case_summary's compacted view (the small
    # H.5 surrogate for the H.7 episodic event log).
    note_marker = f"\n[note @ {time.time():.0f}] {note}"
    new_summary = (case.working_memory_summary or "") + note_marker
    case_store.update(ctx.case_id, working_memory_summary=new_summary)
    return ToolResult(
        tool_name="write_case_note",
        status="ok",
        data={"audit_visible": audit_visible, "note_persisted": True},
    )


def _tool_check_credit(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Wraps CreditHoldReleaseRecipe — deterministic credit-availability
    check. Returns the recipe's clear / blocked verdict + headroom."""
    from recipes.CreditHoldReleaseRecipe import release_credit_hold
    customer_id = args.get("customer_id")
    order_value = args.get("order_value")
    credit_limit = args.get("credit_limit")
    current_exposure = args.get("current_exposure")
    if not all(
        isinstance(v, (int, float))
        for v in (order_value, credit_limit, current_exposure)
    ):
        return ToolResult(
            tool_name="check_credit",
            status="error",
            error="order_value / credit_limit / current_exposure must all be numeric",
        )
    # release_credit_hold expects authorized_roles + exposure_tolerance;
    # for a check_credit call we pass conservative defaults — no role
    # is auto-authorised, tolerance is 0 so the recipe surfaces any
    # over-limit as BLOCKED.
    output = release_credit_hold(
        order_id=args.get("order_id", ctx.case_id),
        requester_role=args.get("requester_role"),
        credit_limit=float(credit_limit),
        current_exposure=float(current_exposure),
        authorized_roles=tuple(),  # no auto-release on a check
        exposure_tolerance=0.0,
    )
    return ToolResult(tool_name="check_credit", status="ok", data=output)


def _tool_check_duplicate_po(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Wraps DuplicatePORecipe — deterministic duplicate-PO classifier."""
    from recipes.DuplicatePORecipe import detect_duplicate_po
    incoming_po = args.get("po_number")
    customer_id = args.get("customer_id", "")
    signal_scores = args.get("signal_scores", {})
    if not isinstance(incoming_po, str) or not isinstance(signal_scores, dict):
        return ToolResult(
            tool_name="check_duplicate_po",
            status="error",
            error="po_number (str) and signal_scores (dict) required",
        )
    output = detect_duplicate_po(
        incoming_po_number=incoming_po,
        customer_id=customer_id,
        signal_scores=signal_scores,
    )
    return ToolResult(tool_name="check_duplicate_po", status="ok", data=output)


def _tool_check_moq(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Wraps MOQRoundUpRecipe — MOQ shortfall classification."""
    from recipes.MOQRoundUpRecipe import round_up_moq
    from contracts.policy import MOQ_SEVERE_SHORTFALL_PCT, MOQ_UPLIFT_REVIEW_PCT
    output = round_up_moq(
        order_id=args.get("order_id", ctx.case_id),
        sku=str(args.get("sku") or ""),
        ordered_qty=float(args.get("ordered_qty") or 0.0),
        moq_qty=float(args.get("moq_qty") or 0.0),
        unit_cost=float(args.get("unit_cost") or 0.0),
        uom=str(args.get("uom") or "CS"),
        severe_shortfall_pct=MOQ_SEVERE_SHORTFALL_PCT,
        uplift_review_pct=MOQ_UPLIFT_REVIEW_PCT,
    )
    return ToolResult(tool_name="check_moq", status="ok", data=output)


def _tool_extract_attachment(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """L2 primitive wrapper. The provider lives in `ctx.extras['multimodal_provider']`
    so production wires the real one and tests pass a stub.
    """
    from agents.primitives.extract_attachment import (
        attachment_ref_from_metadata,
        extract_attachment,
    )
    provider = ctx.extras.get("multimodal_provider")
    if provider is None:
        return ToolResult(
            tool_name="extract_attachment",
            status="error",
            error="multimodal_provider not configured on ToolContext.extras",
        )
    raw = args.get("attachment") or {}
    if not isinstance(raw, dict):
        return ToolResult(
            tool_name="extract_attachment",
            status="error",
            error="`attachment` argument must be a dict (metadata)",
        )
    ref = attachment_ref_from_metadata(ctx.tenant_id, ctx.case_id, raw)
    fields_hint = args.get("fields_hint")
    output = extract_attachment(ref, fields_hint=fields_hint, provider=provider)
    return ToolResult(
        tool_name="extract_attachment",
        status="ok",
        data=output.model_dump(),
    )


def _tool_escalate(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Halt the agent loop and route to MANUAL_REVIEW_REQUIRED.

    The harness reads this result, marks the case
    OPEN_AWAITING_HUMAN, and exits the loop.
    """
    reason_code = args.get("reason_code") or "agent_escalated"
    target_role = args.get("target_role") or "manager"
    return ToolResult(
        tool_name="escalate",
        status="ok",
        data={
            "halt_loop": True,
            "reason_code": str(reason_code),
            "target_role": str(target_role),
        },
    )


def _tool_request_clarification_email(
    ctx: ToolContext, args: Dict[str, Any],
) -> ToolResult:
    """Draft (NOT send) a clarification email back to the buyer.
    Halts the agent loop; the case transitions to OPEN_AWAITING_BUYER.
    """
    template = args.get("template")
    fields = args.get("fields", {})
    if not isinstance(template, str) or not isinstance(fields, dict):
        return ToolResult(
            tool_name="request_clarification_email",
            status="error",
            error="`template` (str) and `fields` (dict) required",
        )
    return ToolResult(
        tool_name="request_clarification_email",
        status="ok",
        data={
            "halt_loop": True,
            "draft": {"template": template, "fields": fields},
            "case_status_after": "OPEN_AWAITING_BUYER",
        },
    )


def _tool_declare_done(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Agent declares the case-event run complete. Harness
    transitions the case to RESOLVED (or the supplied status)."""
    target = args.get("status", "RESOLVED")
    return ToolResult(
        tool_name="declare_done",
        status="ok",
        data={
            "halt_loop": True,
            "case_status_after": str(target),
        },
    )


# ---------------------------------------------------------------------------
# Default registry — built at import time
# ---------------------------------------------------------------------------


def build_default_registry() -> ToolRegistry:
    """Construct the registry the harness uses by default.

    Phase H.5 ships 9 of the 18 tools. Remaining tools (resolve_*,
    check_atp / check_pricing_variance / check_pallet_alignment,
    submit_to_erp, apply_auto_correct, draft_buyer_email,
    load_example, request_compaction, read_case_events) land as
    their backing primitives + L0 bundles arrive.
    """
    registry = ToolRegistry()

    registry.register(ToolSpec(
        name="read_case_summary",
        description="Compacted overview of the current case (~200 tokens). Always cheap.",
        handler=_tool_read_case_summary,
    ))
    registry.register(ToolSpec(
        name="read_extracted_fields",
        description=(
            "Look up previously-extracted attachment fields by template_fingerprint. "
            "Returns not_found when no prior extraction exists; use extract_attachment "
            "afterward."
        ),
        handler=_tool_read_extracted_fields,
    ))
    registry.register(ToolSpec(
        name="extract_attachment",
        description=(
            "Extract structured fields from an attachment. Args: attachment (metadata "
            "dict), fields_hint (optional list[str]). Format-dispatches PDF/Excel/image; "
            "tenant-isolated cache."
        ),
        handler=_tool_extract_attachment,
        cost_estimate_usd=0.020,
    ))
    registry.register(ToolSpec(
        name="check_credit",
        description=(
            "Run the deterministic credit availability check. Args: order_value, "
            "credit_limit, current_exposure (numeric)."
        ),
        handler=_tool_check_credit,
        cost_estimate_usd=0.0001,
    ))
    registry.register(ToolSpec(
        name="check_duplicate_po",
        description=(
            "Run the deterministic duplicate-PO classifier. Args: po_number (str), "
            "customer_id, signal_scores (dict per-signal floats 0..1)."
        ),
        handler=_tool_check_duplicate_po,
        cost_estimate_usd=0.0001,
    ))
    registry.register(ToolSpec(
        name="check_moq",
        description=(
            "Run the deterministic MOQ round-up check. Args: sku, ordered_qty, moq_qty, "
            "unit_cost, uom."
        ),
        handler=_tool_check_moq,
        cost_estimate_usd=0.0001,
    ))
    registry.register(ToolSpec(
        name="write_case_note",
        description=(
            "Persist an agent note onto the case (audit-visible by default). Args: "
            "note (str), audit_visible (bool, optional)."
        ),
        handler=_tool_write_case_note,
    ))
    registry.register(ToolSpec(
        name="request_clarification_email",
        description=(
            "Draft (NOT send) a clarification email to the buyer. Halts the agent loop; "
            "case transitions to OPEN_AWAITING_BUYER. Args: template (str), fields (dict)."
        ),
        handler=_tool_request_clarification_email,
    ))
    registry.register(ToolSpec(
        name="escalate",
        description=(
            "Halt the agent loop and route to MANUAL_REVIEW_REQUIRED. Args: "
            "reason_code (str from L0 vocabulary), target_role (str)."
        ),
        handler=_tool_escalate,
    ))
    registry.register(ToolSpec(
        name="declare_done",
        description=(
            "Agent declares the case-event run complete. Halts the loop. "
            "Args: status (default 'RESOLVED')."
        ),
        handler=_tool_declare_done,
    ))

    return registry
