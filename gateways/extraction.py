from __future__ import annotations

# Order-extraction gateway (ADR-042 Phase 3, ADR-025 reads-before-shadow).
#
# The constrained-generation LLM read that turns an inbound email + its
# attachments into the structured order an operator reviews before ERP submit.
# It is the *producer* that activates the Customer Inbox "Order Entry" and
# "Entities" tabs: its output lands in `state.enrichment_context` under
# `order_entry_extraction` / `inbox_entities`, which the composer
# (`api.profile_composer`) projects into the typed analysis sections.
#
# Architecture (CLAUDE.md Guardrails #1/#3/#6):
#   * One LLM read → one strict `ExtractedOrderEnvelope` (constrained at
#     generation time; free-form parsing is not allowed for machine-consumed
#     output). The eval harness scores *correctness*; the schema guarantees
#     *shape*.
#   * The gateway is a dumb adapter: it sanitizes the untrusted text, calls a
#     pluggable backend, and projects the envelope into enrichment_context
#     keys. No business logic, no composition of a "ready-to-render" payload —
#     the composer owns projection, the UI is a dumb projector.
#   * Backend is swappable: `OutlinesExtractionBackend` (live, constrained
#     generation; needs the heavy `outlines`/`torch` extra) or
#     `RecordedGatewayBackend` (replay of frozen output, for deterministic
#     red-green TDD). Red-green never hits a live model (strategy §3).

from typing import Any, List, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from api.schemas import (
    ExtractedEntity,
    OrderEntryHeader,
    OrderEntryLineItem,
    OrderEntryValidationFlag,
)
from contracts.models import GatewayRequest, GatewayResponse
from llm.sanitizer import sanitize_email_text_for_llm

GATEWAY_NAME = "order_extraction"
OP_EXTRACT_ORDER = "extract_order"
OP_EXTRACT_ENTITIES = "extract_entities"


class ExtractedOrderEnvelope(BaseModel):
    """Constrained-generation output contract for the order-extraction gateway.

    One LLM read produces exactly this strict object. The gateway projects it
    into the two enrichment_context shapes the composer validates: the order
    form (`order_entry_extraction`) and the pulled-out entities
    (`inbox_entities`). The sub-models are reused verbatim from `api.schemas`
    so the recorded-fixture → gateway → enrichment_context → composer
    round-trip cannot drift.
    """

    model_config = ConfigDict(extra="forbid")

    source_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    header: OrderEntryHeader
    customer_name: Optional[str] = None
    customer_bp: Optional[str] = None
    line_items: List[OrderEntryLineItem] = Field(default_factory=list)
    validation_flags: List[OrderEntryValidationFlag] = Field(default_factory=list)
    entities: List[ExtractedEntity] = Field(default_factory=list)

    def to_order_entry_extraction(self) -> dict[str, Any]:
        """Project to the `enrichment_context["order_entry_extraction"]` shape
        (validated by `compose_order_entry_extraction`)."""
        return {
            "source_type": self.source_type,
            "confidence": self.confidence,
            "header": self.header.model_dump(),
            "customer_name": self.customer_name,
            "customer_bp": self.customer_bp,
            "line_items": [li.model_dump() for li in self.line_items],
            "validation_flags": [v.model_dump() for v in self.validation_flags],
        }

    def to_inbox_entities(self) -> dict[str, Any]:
        """Project to the `enrichment_context["inbox_entities"]` shape
        (validated by `compose_entities_analysis`)."""
        return {"extracted": [e.model_dump() for e in self.entities]}


@runtime_checkable
class ExtractionBackend(Protocol):
    """The constrained-generation surface the gateway depends on.

    `safe_text` is already sanitized (untrusted-data fenced); `source_type`
    is the inbound channel/format; `hint` carries request params a replay
    backend uses to resolve a recording (live backends ignore it).
    """

    def extract_order(
        self, *, safe_text: str, source_type: str, hint: Mapping[str, Any]
    ) -> ExtractedOrderEnvelope: ...


class OrderExtractionGateway:
    """InfrastructureGateway adapter wrapping a constrained-generation backend.

    Resolved by `orchestration.nodes.resolve_dependencies` before shadow
    (ADR-025). Sanitizes the untrusted email/attachment text (DoR #1) and then
    delegates to the backend; the structured envelope is projected per
    operation into the enrichment_context payload the composer reads.
    """

    def __init__(self, backend: ExtractionBackend) -> None:
        self._backend = backend

    @property
    def name(self) -> str:
        return GATEWAY_NAME

    def execute(self, request: GatewayRequest) -> GatewayResponse:
        if request.operation not in (OP_EXTRACT_ORDER, OP_EXTRACT_ENTITIES):
            return GatewayResponse(
                gateway_name=self.name,
                operation=request.operation,
                status="UNAVAILABLE",
                error=f"Unknown operation: {request.operation!r}",
            )

        params = request.params or {}
        raw_body = str(params.get("email_body") or "")
        raw_attach = str(params.get("attachment_text") or "")
        source_type = str(params.get("source_type") or "EMAIL_BODY")

        # DoR #1 — fence the attacker-controlled free text BEFORE the model
        # sees it. The gateway is the trust boundary; the backend only ever
        # receives sanitized, delimited input.
        safe_text = sanitize_email_text_for_llm(raw_body, attachment_text=raw_attach)

        # Backend failures (e.g. a missing recording, a model error) propagate
        # to the GatewayExecutor, which maps them to a FAILED response so
        # resolve_dependencies can apply its required_for_audit policy. We do
        # not swallow them here — a silent empty extraction is partial truth.
        envelope = self._backend.extract_order(
            safe_text=safe_text, source_type=source_type, hint=params
        )

        if request.operation == OP_EXTRACT_ORDER:
            data = envelope.to_order_entry_extraction()
        else:  # OP_EXTRACT_ENTITIES
            data = envelope.to_inbox_entities()

        return GatewayResponse(
            gateway_name=self.name,
            operation=request.operation,
            status="SUCCESS",
            data=data,
        )

    def health_check(self) -> bool:
        return True
