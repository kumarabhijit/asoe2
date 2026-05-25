"""Duplicate-PO metadata contract (ADR-028 Guard-rail 1, action item A5).

Pydantic submodels + runtime validators that enforce the keys/types
documented in ``docs/specs/duplicate-po/metadata-contract.md`` for:

  * ``OrderEvent.metadata`` when intent == DUPLICATE_PO     (input contract)
  * ``ExecutionLog.outputs`` from DuplicatePORecipe.py      (output contract)

V1 enforcement runs at the orchestration tail (``build_analysis``):
violations route to ``TerminalStatus.AUDIT_CONTEXT_MISSING`` with an
explanation that names the offending key(s) so auditors see the
specific contract failure rather than a generic crash.

V1.5 deferred: a write-time check inside ``db/repository.py`` that
refuses to persist a JSONB payload whose shape violates the contract.
The orchestration-tail check above is the first line of defense; a
DB-level check would be the last line of defense (covers any path
that writes to the exception store outside the standard graph). See
``metadata-contract.md`` §V1.5 for the planned wiring.

Why Pydantic submodels here and not in api/schemas.py?

  * ``api/schemas.py::*AnalysisData`` are READ-side projections owned
    by the composer (``api/analysis_composer.py``) — they describe what
    the UI consumes.
  * The contract here is WRITE-side: what the upstream classifier and
    recipe must produce. Different role, different ownership, lives
    alongside ``contracts/models.py``.

Adding new keys: extend ``DuplicatePOEventMetadata`` /
``DuplicatePORecipeOutput`` AND update the metadata-contract.md table
in the same PR. Tests in ``tests/test_metadata_contract.py`` will
fail until both sides match.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from constraints.specs import AllowedResolutionAction


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class MetadataContractViolation(ValueError):
    """Raised when a DUPLICATE_PO event metadata or recipe output payload
    violates the documented contract.

    Carries a list of ``(key, reason)`` tuples so the orchestration layer
    can construct a precise explanation for the AUDIT_CONTEXT_MISSING
    routing — auditors see exactly which field broke the contract, not
    a generic Pydantic stack trace.
    """

    def __init__(
        self,
        contract_name: str,
        offenders: List[Tuple[str, str]],
    ) -> None:
        self.contract_name = contract_name
        self.offenders = offenders
        # Human-readable summary suitable for an audit-trail explanation.
        bullet_list = "; ".join(f"{key}: {reason}" for key, reason in offenders)
        super().__init__(
            f"{contract_name} contract violation — {bullet_list}"
        )


# ---------------------------------------------------------------------------
# Input contract — OrderEvent.metadata for DUPLICATE_PO events
# ---------------------------------------------------------------------------


class DuplicatePOEventMetadata(BaseModel):
    """Allowed keys on ``OrderEvent.metadata`` when the event routes to
    ``DuplicatePORecipe.py``.

    ``model_config = extra="allow"`` is intentional — cross-cutting
    metadata (tracing IDs, debug flags, propagation values) may legitimately
    appear alongside the contract fields. The contract rejects only:
      * Missing required keys
      * Wrong types on declared keys
      * Out-of-range values on declared keys

    Cross-cutting keys flow through opaquely; they don't influence the
    recipe's scoring or routing per ADR-028 G1's "forbidden keys (V1)"
    clause: "Any key not listed above whose value is consumed by the
    recipe or its dependencies." The recipe consumes only the keys
    declared here, so anything else is by definition not influencing
    behaviour.
    """

    model_config = ConfigDict(extra="allow")

    signal_scores: Dict[str, float] = Field(
        ...,
        description=(
            "Per-signal match scores in [0.0, 1.0]. Expected keys: "
            "po_number, customer_id, line_items, amount, timestamp, "
            "ship_to, channel, delivery_date. Missing keys default to "
            "0.0 in the recipe (conservative). Source: upstream "
            "classifier."
        ),
    )

    matched_po_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the existing PO this event is being scored "
            "against. Surfaces in the canonical envelope (ADR-028 G2). "
            "Source: upstream classifier."
        ),
    )

    tenant_id: Optional[str] = Field(
        None,
        description=(
            "Tenant identifier. Optional in V1 — the file-backed resolver "
            "(gateways/tenant_config.py) does not consume this. A9 will "
            "extend the resolver to use it once tenant_config table lands."
        ),
    )

    customer_tier: Optional[Literal["strategic", "standard", "smb"]] = Field(
        None,
        description=(
            "Customer tier for L3 overrides. V1 file-backed resolver "
            "carries no tier-level weight overrides; A9 will populate."
        ),
    )

    channel: Optional[str] = Field(
        None,
        description=(
            "Source channel identifier (e.g. 'EDI', 'PORTAL'). V1 "
            "file-backed resolver carries no channel-level weight "
            "overrides; A9 will populate."
        ),
    )

    behavior_tag: Optional[Literal["blanket_po", "drop_ship", "high_frequency"]] = Field(
        None,
        description=(
            "Behavior tag selecting a preset L4 partial weight override "
            "from customer_behavior_overrides in gateways/configs/duplicate_po/defaults.json."
        ),
    )


# Expected signal_scores keys per the recipe's _WEIGHTS map. Kept here
# (not imported from the recipe) so the contract module is independent
# of the recipe — a contract assertion that wraps an import would create
# a cycle if a recipe later imported the contract.
_EXPECTED_SIGNAL_KEYS: Tuple[str, ...] = (
    "po_number", "customer_id", "line_items", "amount",
    "timestamp", "ship_to", "channel", "delivery_date",
)


def validate_duplicate_po_event_metadata(
    metadata: Dict[str, Any],
) -> DuplicatePOEventMetadata:
    """Validate raw event-metadata against the input contract.

    Returns the parsed model on success. Raises
    ``MetadataContractViolation`` with a per-key offender list on
    failure — the caller uses ``exc.offenders`` to build the
    AUDIT_CONTEXT_MISSING explanation.

    The Pydantic ``ValidationError`` is intentionally caught and
    re-raised as ``MetadataContractViolation`` so the orchestration
    layer has a single domain exception to handle (compare to the
    ``WeightContractViolation`` pattern in
    ``recipes/DuplicatePORecipe.py``).
    """
    offenders: List[Tuple[str, str]] = []

    try:
        parsed = DuplicatePOEventMetadata.model_validate(metadata)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            offenders.append((loc, err["msg"]))
        raise MetadataContractViolation(
            "OrderEvent.metadata (DUPLICATE_PO)", offenders
        ) from exc

    # signal_scores key-set + value-range checks live outside the
    # Pydantic model so we can produce per-key diagnostics rather than
    # a generic Dict[str, float] complaint. Pydantic accepts any
    # signal_scores dict with float values; we narrow further here.
    scores = parsed.signal_scores
    extra = sorted(set(scores.keys()) - set(_EXPECTED_SIGNAL_KEYS))
    if extra:
        offenders.append(
            ("signal_scores", f"unknown signal keys: {extra}"),
        )
    for key, value in scores.items():
        if not isinstance(value, (int, float)):
            offenders.append(
                (f"signal_scores.{key}", f"not numeric: {value!r}"),
            )
            continue
        if value < 0.0 or value > 1.0:
            offenders.append(
                (f"signal_scores.{key}", f"value {value!r} outside [0.0, 1.0]"),
            )

    if offenders:
        raise MetadataContractViolation(
            "OrderEvent.metadata (DUPLICATE_PO)", offenders
        )
    return parsed


# ---------------------------------------------------------------------------
# Output contract — ExecutionLog.outputs from DuplicatePORecipe.py
# ---------------------------------------------------------------------------


_RECIPE_STATUS = Literal["BLOCKED", "REVIEW_REQUIRED", "SOFT_FLAG", "PASS"]
_RECIPE_CLASSIFICATION = Literal[
    "AUTO_BLOCK", "REVIEW_REQUIRED", "SOFT_FLAG", "PASS"
]
_AUTONOMY_LEVEL = Literal["L1", "L2", "L3", "L4"]


class DuplicatePORecipeOutput(BaseModel):
    """Required keys on ``ExecutionLog.outputs`` when the executed recipe
    is ``DuplicatePORecipe.py``.

    ``model_config = extra="forbid"`` is intentional — the recipe is
    deterministic and its output shape is fixed. Any extra key indicates
    drift between the recipe's documented surface and what it actually
    emits, which is a contract bug regardless of intent.
    """

    model_config = ConfigDict(extra="forbid")

    status: _RECIPE_STATUS
    composite_score: float = Field(..., ge=0.0, le=1.0)
    classification: _RECIPE_CLASSIFICATION
    recommended_action: AllowedResolutionAction
    autonomy_level: Optional[_AUTONOMY_LEVEL] = None
    # Stamped by the orchestration layer (execute_recipe) alongside
    # autonomy_level so the record self-describes which autonomy vocabulary its
    # level resolves under (ADR-042 §5). Absent on records that predate the
    # versioned vocabulary — those resolve under v1.
    autonomy_vocab_version: Optional[str] = None
    notification_template: Optional[str] = None
    signal_breakdown: Dict[str, float]
    incoming_po_number: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=0)


def validate_duplicate_po_recipe_output(
    outputs: Dict[str, Any],
) -> DuplicatePORecipeOutput:
    """Validate raw recipe-output dict against the output contract.

    Returns the parsed model on success. Raises
    ``MetadataContractViolation`` with a per-key offender list on
    failure — the orchestration layer uses ``exc.offenders`` to build
    the AUDIT_CONTEXT_MISSING explanation.
    """
    offenders: List[Tuple[str, str]] = []

    try:
        parsed = DuplicatePORecipeOutput.model_validate(outputs)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            offenders.append((loc, err["msg"]))
        raise MetadataContractViolation(
            "ExecutionLog.outputs (DuplicatePORecipe.py)", offenders
        ) from exc

    # signal_breakdown key-set check — same expected keys as the input
    # signal_scores. Defensive symmetry: the recipe should emit a
    # breakdown for exactly the eight canonical signals.
    extra = sorted(set(parsed.signal_breakdown.keys()) - set(_EXPECTED_SIGNAL_KEYS))
    missing = sorted(set(_EXPECTED_SIGNAL_KEYS) - set(parsed.signal_breakdown.keys()))
    if extra:
        offenders.append(
            ("signal_breakdown", f"unknown signal keys: {extra}"),
        )
    if missing:
        offenders.append(
            ("signal_breakdown", f"missing signal keys: {missing}"),
        )
    for key, value in parsed.signal_breakdown.items():
        if not isinstance(value, (int, float)):
            offenders.append(
                (f"signal_breakdown.{key}", f"not numeric: {value!r}"),
            )

    if offenders:
        raise MetadataContractViolation(
            "ExecutionLog.outputs (DuplicatePORecipe.py)", offenders
        )
    return parsed
