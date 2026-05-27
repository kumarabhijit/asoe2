"""Analysis composer — registry-enforced projection.

Counterpart to `api/analysis_adapters.py`. The adapter projects a
recipe output into a Pydantic model; the composer wraps that
projection with a coverage check against
`compliance/audit_bearing_registry.yaml`. When one or more
audit-bearing fields declared in the registry cannot be populated,
the composer returns a structured diagnostic that the graph-level
`build_analysis` node (or the /analysis endpoint, in its read path)
consumes to route the record to `TerminalStatus.AUDIT_CONTEXT_MISSING`.

Design invariants:

* The registry is the ONLY authority on which fields are audit-bearing.
  This module never hard-codes a field classification — every decision
  reads from the YAML.
* `conditional` fields evaluate their `depends_on` predicate against
  `record.resolved_action`. When the predicate holds, the field is
  audit-bearing; otherwise it's contextual (its absence is fine).
* `grandfather_clauses` name fields that are classified audit-bearing
  by compliance intent but have no current producer. Until the
  clause's deadline passes, the composer treats those fields as
  contextual (to avoid blocking production), then flips them to
  audit-bearing-enforced post-deadline. The deadline check is pure
  date-math — no feature flags, no environment overrides.
* The composer is pure — a function `compose(record)` with no
  persistence side effects. Persistence is the graph node's job.

The read path (/analysis endpoint) currently calls the adapter
directly; the composer is the upgrade path. Integration happens in
a later commit (graph node + endpoint wiring) to keep this module
reviewable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel

from api.analysis_adapters import ANALYSIS_ADAPTERS, resolve_adapter_key
from api.store import ChildCase

# --------------------------------------------------------------------------
# Registry load — happens once at module import. The file is part of the
# repo (not external config) so there is no failure mode worth handling
# beyond crashing the process — a missing registry means compliance
# enforcement is broken, which must be visible immediately.
# --------------------------------------------------------------------------

_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "compliance" / "audit_bearing_registry.yaml"
)

with _REGISTRY_PATH.open("r", encoding="utf-8") as _f:
    _REGISTRY: Dict[str, Any] = yaml.safe_load(_f)

_ALWAYS_AUDIT_BEARING_NAMES: set[str] = set(
    _REGISTRY.get("conventions", {}).get("always_audit_bearing_field_names", [])
)


@dataclass(frozen=True)
class FieldClassification:
    """One field's entry from the registry, normalised."""

    name: str
    tier: str  # audit-bearing | conditional | contextual
    depends_on: Optional[str] = None
    grandfathered_until: Optional[date] = None


@dataclass
class ComposedAnalysis:
    """Result of composing one record's enrichment field.

    Attributes:
        projection: The typed Pydantic model the adapter produced
            (may be None if the adapter itself returned None).
        missing_audit_fields: Names of audit-bearing fields declared
            in the registry that are absent on the projection. Empty
            when coverage is complete.
        waived_fields: Fields that are classified audit-bearing but
            currently waived via an active grandfather clause.
        class_name: The Pydantic class registered for this record's
            recipe (e.g., "PriceHoldAnalysisData"). None if the
            record has no adapter.
    """

    projection: Optional[BaseModel] = None
    missing_audit_fields: List[str] = field(default_factory=list)
    waived_fields: List[str] = field(default_factory=list)
    class_name: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        """True when every audit-bearing field was populated."""
        return not self.missing_audit_fields

    @property
    def should_route_to_audit_context_missing(self) -> bool:
        """Enforcement hook for the graph node. True when the record
        has a registry-declared adapter AND at least one audit-bearing
        field is absent AND that field is not currently
        grandfathered."""
        return self.class_name is not None and not self.is_complete


def _today() -> date:
    """Module-level seam for tests to override 'today'. Real code
    should not call this directly; callers pass a date or rely on the
    default."""
    return date.today()


def _load_grandfathered_fields(today: Optional[date] = None) -> set[str]:
    """Return the set of dotted field names (e.g.,
    "PriceAnalysisData.contract_ref") whose grandfather clause is
    still active as of `today`. Past-deadline clauses drop off
    automatically — no code change needed when a clause expires."""
    effective = today or _today()
    waived: set[str] = set()
    clauses = _REGISTRY.get("grandfather_clauses", {}) or {}
    for clause in clauses.values():
        deadline = clause.get("deadline")
        if isinstance(deadline, date) and deadline >= effective:
            waived.update(clause.get("fields", []))
    return waived


def _classify_field(
    class_name: str, field_name: str, field_entry: Dict[str, Any],
    waived: set[str],
) -> FieldClassification:
    """Normalise one registry row into a FieldClassification.

    Applies the `conventions.always_audit_bearing_field_names` rule
    (so a new recipe that adds an `action` field inherits the
    classification without a per-field row having to assert it).
    """
    tier = field_entry.get("tier", "contextual")
    if field_name in _ALWAYS_AUDIT_BEARING_NAMES and tier == "contextual":
        # Conventions can only upgrade, never downgrade.
        tier = "audit-bearing"
    depends_on = field_entry.get("depends_on")
    return FieldClassification(
        name=field_name,
        tier=tier,
        depends_on=depends_on,
        grandfathered_until=(
            date.max if f"{class_name}.{field_name}" in waived else None
        ),
    )


def _resolve_action_of(record: ChildCase) -> Optional[str]:
    """Look up the resolved action for conditional-field evaluation.

    A `conditional` field's `depends_on` string like
    `"resolved_action == ALT_DC"` is checked against this value. The
    field stays contextual while the action is absent or doesn't
    match — matching the UX researcher's "hide what wasn't used"
    principle.
    """
    return getattr(record, "resolved_action", None) or None


def _predicate_holds(
    depends_on: str, resolved_action: Optional[str],
) -> bool:
    """Evaluate a registry predicate. v1 supports only the
    `resolved_action == <VALUE>` and `resolved_action in <list>` forms
    — sufficient for every conditional field in today's registry.
    Unknown predicate shapes return False (fail-closed: if we can't
    evaluate it, treat the field as contextual)."""
    text = depends_on.strip()
    if text.startswith("resolved_action == "):
        expected = text[len("resolved_action == "):].strip()
        return resolved_action == expected
    if text.startswith("resolved_action in "):
        # Tolerate whitespace / bracket style — this is policy text
        # shipped by compliance, not machine-generated.
        inside = text[len("resolved_action in "):].strip().strip("[]")
        choices = {c.strip().strip('"').strip("'") for c in inside.split(",")}
        return resolved_action in choices
    return False


def _iter_registry_fields(
    class_name: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    """All rows for a registry section, in declaration order. Returns
    empty list when the class is unknown — callers treat that as
    "no enforcement" (the adapter still runs)."""
    section = _REGISTRY.get(class_name)
    if not isinstance(section, dict):
        return []
    return [
        (name, entry)
        for name, entry in section.items()
        if isinstance(entry, dict) and "tier" in entry
    ]


def _required_audit_fields(
    record: ChildCase, class_name: str,
) -> Tuple[List[str], List[str]]:
    """Split the class's audit-bearing fields into (enforced,
    grandfathered). Enforced fields MUST be populated for coverage to
    pass; grandfathered fields are still declared audit-bearing in the
    registry but waived for this record until the clause deadline.
    """
    waived = _load_grandfathered_fields()
    resolved = _resolve_action_of(record)
    enforced: List[str] = []
    grandfathered: List[str] = []
    for name, entry in _iter_registry_fields(class_name):
        cls = _classify_field(class_name, name, entry, waived)
        if cls.tier == "contextual":
            continue
        if cls.tier == "conditional":
            if not cls.depends_on or not _predicate_holds(cls.depends_on, resolved):
                continue
        # tier is audit-bearing OR an activated conditional.
        if cls.grandfathered_until is not None:
            grandfathered.append(name)
        else:
            enforced.append(name)
    return enforced, grandfathered


def _populated(model: BaseModel, field_name: str) -> bool:
    """Is `field_name` on `model` populated with a non-empty value?

    "Empty" mirrors the UI's data-presence guard: None, empty string,
    empty list/dict all count as absent. Nested Pydantic models are
    only checked for presence, not deep-required-subfield recursion —
    the registry can declare the nested class's own section if that
    is required.
    """
    if not hasattr(model, field_name):
        return False
    value = getattr(model, field_name)
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple, set)) and len(value) == 0:
        return False
    return True


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def compose(record: ChildCase) -> ComposedAnalysis:
    """Project one record's enrichment and validate registry coverage.

    Returns a ComposedAnalysis with:
      * `projection` — the adapter's typed Pydantic model (or None).
      * `missing_audit_fields` — registry-declared audit-bearing
        fields absent on the projection. Empty = coverage complete.
      * `waived_fields` — fields classified audit-bearing but
        currently active under a grandfather clause.
      * `class_name` — the Pydantic class in play, or None if no
        adapter is registered.

    Callers:
      * `/analysis` endpoint (read-only) — surfaces the projection
        when coverage is complete; returns None otherwise and logs
        the missing fields as a diagnostic.
      * Future `build_analysis` graph node — on
        `should_route_to_audit_context_missing`, sets
        `state.final_status = TerminalStatus.AUDIT_CONTEXT_MISSING`
        with the missing fields captured in `state.explanation`.
    """
    key = resolve_adapter_key(record)
    if not key or key not in ANALYSIS_ADAPTERS:
        return ComposedAnalysis()

    _field_name, adapter = ANALYSIS_ADAPTERS[key]
    projection = adapter(record)

    class_name = _pydantic_class_name_for(key)
    if class_name is None:
        # Adapter is wired but the registry has no section for its
        # target type. v1 of the registry may not yet cover every
        # adapter — treat as "no enforcement" rather than crashing.
        return ComposedAnalysis(projection=projection)

    enforced, grandfathered = _required_audit_fields(record, class_name)
    if projection is None:
        # Nothing to check against — every enforced field is missing.
        return ComposedAnalysis(
            projection=None,
            missing_audit_fields=enforced,
            waived_fields=grandfathered,
            class_name=class_name,
        )

    missing = [f for f in enforced if not _populated(projection, f)]
    return ComposedAnalysis(
        projection=projection,
        missing_audit_fields=missing,
        waived_fields=grandfathered,
        class_name=class_name,
    )


def _pydantic_class_name_for(registry_key: str) -> Optional[str]:
    """Map an ANALYSIS_ADAPTERS key (recipe name) to the registry
    section name (Pydantic class). The adapters use
    `from __future__ import annotations`, so we resolve type hints
    via `typing.get_type_hints()` rather than raw
    `__annotations__` (which would be strings).
    """
    import typing
    from api.analysis_adapters import ANALYSIS_ADAPTERS  # avoid cycles

    entry = ANALYSIS_ADAPTERS.get(registry_key)
    if entry is None:
        return None
    _field, adapter = entry
    try:
        hints = typing.get_type_hints(adapter)
    except Exception:
        return None
    return_type = hints.get("return")
    if return_type is None:
        return None
    # Unwrap `Optional[X]` / `X | None` to get `X`.
    args = typing.get_args(return_type)
    if args:
        for arg in args:
            if arg is type(None):
                continue
            name = getattr(arg, "__name__", None)
            if name:
                return name
    return getattr(return_type, "__name__", None)


def compose_from_state(state: Any) -> ComposedAnalysis:
    """Graph-time entry point. Builds an ephemeral record-shaped view
    over a `GraphState` and delegates to `compose(record)`.

    Used by the `build_analysis` graph node (orchestration.nodes) to
    enforce registry coverage BEFORE persistence. At that point the
    record doesn't exist yet — _persist_exception only runs after
    the graph. The view exposes exactly the attributes the composer
    reads:

      * selected_recipe          — resolve_adapter_key()
      * intent (as str)          — resolve_adapter_key() fallback
      * resolution_data          — adapter's GREEN path
      * original_event           — adapter's synthetic fallback
      * enrichment_context       — (reserved for Pillar 1 consumers)
      * resolved_action          — conditional predicate evaluation

    The view is a dataclass-like stub, not a real ChildCase —
    it doesn't persist, doesn't have an id, doesn't need a tenant.
    That's intentional: we're enforcing the registry, not creating
    an audit row.

    Note on `resolved_action`: this is hard-coded to None in the
    view below because at graph-execution time the disposition
    hasn't happened yet, so conditional fields gated on the
    operator's resolved_action (alternate_warehouses,
    substitutes, production, inbound_po) are correctly classified
    as not-yet-required. Post-disposition coverage IS enforced —
    the read-path composer (`compose(record)`, called from the
    /analysis endpoint and from the trace audit-gap snapshot in
    routes/exceptions.py) reads `record.resolved_action` via
    `_resolve_action_of`, so the registry's `depends_on:
    resolved_action == X` predicates fire once the operator picks
    an action. This split keeps graph-time enforcement honest
    (no false-positive AUDIT_CONTEXT_MISSING for fields the
    operator hasn't yet committed to needing) while still gating
    the post-disposition read path.
    """
    from dataclasses import dataclass as _dc, field as _field

    @_dc
    class _StateView:
        selected_recipe: Optional[str]
        intent: Optional[str]
        resolution_data: Dict[str, Any]
        original_event: Optional[Dict[str, Any]]
        enrichment_context: Dict[str, Any] = _field(default_factory=dict)
        resolved_action: Optional[str] = None

    intent_value: Optional[str] = None
    intent = getattr(state, "intent", None)
    if intent is not None:
        # Tolerate both enum and plain-string intents.
        intent_value = (
            intent.value if hasattr(intent, "value") else str(intent)
        )

    outputs: Dict[str, Any] = {}
    exec_log = getattr(state, "execution_log", None)
    if exec_log is not None:
        outputs = getattr(exec_log, "outputs", {}) or {}

    event = getattr(state, "event", None)
    event_dump = (
        event.model_dump(mode="json")
        if event is not None and hasattr(event, "model_dump")
        else None
    )

    view = _StateView(
        selected_recipe=getattr(state, "selected_recipe", None),
        intent=intent_value,
        resolution_data=outputs,
        original_event=event_dump,
        enrichment_context=dict(getattr(state, "enrichment_context", {}) or {}),
        resolved_action=None,  # pre-disposition; HITL hasn't landed an action
    )
    return compose(view)


__all__ = ["ComposedAnalysis", "compose", "compose_from_state"]
