from __future__ import annotations

# Provider-agnostic LLM-backed constraint backend.
#
# Wraps any LLMProviderClient (Anthropic / OpenAI / Google / Ollama /
# HuggingFace) and exposes the same trio surface as
# DeterministicFallbackBackend:
#   - classify_intent(state)  → IntentDecision
#   - propose_recipe(state)   → RecipeProposal | None
#   - shadow_decision(state)  → ShadowDecisionSchema
#
# Composition:
#   sanitizer  → strips OrderEvent.metadata to an allowlisted, length-
#                capped view; wraps in untrusted-data delimiters.
#   breaker    → checked before every call; record_success / failure
#                after.
#   budget     → snapshot before; consume after with cost computed
#                from observed token usage and policy pricing.
#   provider   → vendor-agnostic call_with_tool().
#
# Failure handling: on ProviderError, CircuitOpen, budget hard-block,
# Pydantic validation error, or any unexpected exception, the backend
# DELEGATES to the injected fallback backend (default:
# DeterministicFallbackBackend) for that single trio call. The graph
# never sees a remote-LLM failure — it sees a normal IntentDecision /
# RecipeProposal / ShadowDecisionSchema. This preserves CLAUDE.md §1.4
# (Compliance Shadow gating) and §5 (explicit failure as success state).
#
# Determinism for caching: the system prompt is composed from the
# verbatim SKILL.md catalog (loaded once per process) plus a static
# per-method directive. No state-derived data is ever injected into
# the system prompt or tool schema — the cache-invalidator audit in
# tests/test_llm_cache_invalidators.py asserts byte-identical
# rendered prefixes across distinct OrderEvents.

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from contracts.models import GraphState, Intent, LLMCallTrace
from contracts.policy import LLMTask
from constraints.fallback_backend import DeterministicFallbackBackend
from constraints.specs import (
    IntentDecision,
    RecipeProposal,
    ShadowDecisionSchema,
)
from llm.budget import (
    InMemoryBudgetTracker,
    RedisBudgetTracker,
    TokenUsage,
    estimate_cost_usd,
    get_budget_tracker,
)
from llm.circuit_breaker import CircuitOpen, LLMCircuitBreaker, get_circuit_breaker
from llm.provider_protocol import (
    CacheControl,
    LLMProviderClient,
    ProviderError,
    SystemBlock,
)
from llm.sanitizer import (
    UNTRUSTED_DATA_PREAMBLE,
    render_untrusted_block,
    sanitize_metadata_for_llm,
)

logger = logging.getLogger("asoe.constraints.llm_backend")


# ---------------------------------------------------------------------------
# Skill-catalog loader (cached at module import for cache-friendliness)
# ---------------------------------------------------------------------------


def _load_skill_catalog(skills_dir: str = "skills") -> str:
    """Load every `skills/*.md` and concatenate alphabetically.

    The result becomes the cacheable head of the system prompt for
    every LLM call. Sorting alphabetically guarantees the byte
    sequence is stable across processes — required for prompt-cache
    hits to compose across worker pods.
    """
    root = Path(skills_dir)
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.glob("*.md")):
        parts.append(f"<!-- {path.name} -->\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


_SKILL_CATALOG_CACHE: dict[str, str] = {}


def _get_skill_catalog(skills_dir: str = "skills") -> str:
    if skills_dir not in _SKILL_CATALOG_CACHE:
        _SKILL_CATALOG_CACHE[skills_dir] = _load_skill_catalog(skills_dir)
    return _SKILL_CATALOG_CACHE[skills_dir]


# ---------------------------------------------------------------------------
# Per-task tool descriptors — fully static (cache-safe).
# ---------------------------------------------------------------------------


_INTENT_TOOL_NAME = "classify_intent"
_INTENT_TOOL_DESCRIPTION = (
    "Return a single IntentDecision for the order-to-cash exception "
    "event presented in the user message. Pick exactly one value from "
    "the AllowedIntent enum. Set `confidence` between 0.0 and 1.0. "
    "Set `rationale` to a short, neutral, plain-text explanation."
)
_INTENT_DIRECTIVE = (
    "## Task: classify_intent\n"
    "Classify the order-to-cash exception event into exactly one intent "
    "from AllowedIntent. Use the `classify_intent` tool. Do not narrate "
    "your reasoning outside the tool call."
)

_RECIPE_TOOL_NAME = "propose_recipe"
_RECIPE_TOOL_DESCRIPTION = (
    "Return a RecipeProposal naming the deterministic recipe to invoke "
    "for the already-classified intent. Pick exactly one value from "
    "AllowedRecipeName."
)
_RECIPE_DIRECTIVE = (
    "## Task: propose_recipe\n"
    "Select the deterministic recipe filename matching the classified "
    "intent. Use the `propose_recipe` tool. Do not invent recipes."
)

_SHADOW_TOOL_NAME = "shadow_decision"
_SHADOW_TOOL_DESCRIPTION = (
    "Return a ShadowDecisionSchema verdict for the proposed action. "
    "`status` must be one of GREEN / YELLOW / RED. `reasons` is a list "
    "of short strings. `policy_hits` is a list of policy identifiers."
)
_SHADOW_DIRECTIVE = (
    "## Task: shadow_decision\n"
    "Audit the proposed action against ASOE policy. Return GREEN if "
    "automated execution is clearly safe, YELLOW if a human review is "
    "required, RED to halt. Use the `shadow_decision` tool."
)

# Map LLMTask → (tool_name, tool_description, directive_text, schema_cls).
_TASK_DESCRIPTORS: dict[str, tuple[str, str, str, type]] = {
    "intent": (
        _INTENT_TOOL_NAME,
        _INTENT_TOOL_DESCRIPTION,
        _INTENT_DIRECTIVE,
        IntentDecision,
    ),
    "recipe": (
        _RECIPE_TOOL_NAME,
        _RECIPE_TOOL_DESCRIPTION,
        _RECIPE_DIRECTIVE,
        RecipeProposal,
    ),
    "shadow": (
        _SHADOW_TOOL_NAME,
        _SHADOW_TOOL_DESCRIPTION,
        _SHADOW_DIRECTIVE,
        ShadowDecisionSchema,
    ),
}


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


_BudgetTracker = InMemoryBudgetTracker | RedisBudgetTracker


class RemoteLLMBackend:
    """Constraint backend that forwards trio calls to an LLMProviderClient.

    Construction:
        RemoteLLMBackend(provider_client) — uses module singletons for
            budget tracker, circuit breaker, and the deterministic
            fallback. Production path.
        RemoteLLMBackend(
            provider_client,
            fallback_backend=...,
            budget=...,
            breaker=...,
            skills_dir=...,
        ) — full injection for tests.

    The backend is stateless beyond the injected dependencies and is
    safe to share across threads.
    """

    def __init__(
        self,
        provider_client: LLMProviderClient,
        fallback_backend: Any | None = None,
        *,
        budget: _BudgetTracker | None = None,
        breaker: LLMCircuitBreaker | None = None,
        skills_dir: str = "skills",
        max_tokens: int = 256,
    ) -> None:
        self._provider = provider_client
        self._fallback = fallback_backend or DeterministicFallbackBackend()
        self._budget = budget if budget is not None else get_budget_tracker()
        self._breaker = breaker if breaker is not None else get_circuit_breaker()
        self._skill_catalog = _get_skill_catalog(skills_dir)
        self._skill_md_version = (
            hashlib.sha256(self._skill_catalog.encode("utf-8")).hexdigest()
            if self._skill_catalog
            else ""
        )
        self._max_tokens = max_tokens
        # Per-call trace populated after every _invoke. Orchestration
        # nodes drain it onto state.llm_call_traces and reset to None
        # after each trio call. Never holds more than one entry — the
        # backend itself is stateless across calls.
        self.last_call_trace: Optional[LLMCallTrace] = None

    # ------------------------------------------------------------------
    # Trio surface
    # ------------------------------------------------------------------

    def classify_intent(self, state: GraphState) -> IntentDecision:
        decision = self._invoke(
            task="intent",
            state=state,
            user_message_builder=_build_intent_user_message,
        )
        if isinstance(decision, IntentDecision):
            return decision
        # Fallback path. The fallback backend is guaranteed to return
        # an IntentDecision (it's pure code).
        return self._fallback.classify_intent(state)

    def propose_recipe(self, state: GraphState) -> Optional[RecipeProposal]:
        # Compliance Shadow design: MASS_PRICING_ERROR has no recipe by
        # design (it's a routing-to-human verdict). Short-circuit so we
        # don't spend an LLM call to learn what is structurally true.
        if state.intent == Intent.MASS_PRICING_ERROR:
            return None
        decision = self._invoke(
            task="recipe",
            state=state,
            user_message_builder=_build_recipe_user_message,
        )
        if isinstance(decision, RecipeProposal):
            return decision
        return self._fallback.propose_recipe(state)

    def shadow_decision(self, state: GraphState) -> ShadowDecisionSchema:
        decision = self._invoke(
            task="shadow",
            state=state,
            user_message_builder=_build_shadow_user_message,
        )
        if isinstance(decision, ShadowDecisionSchema):
            return decision
        return self._fallback.shadow_decision(state)

    # ------------------------------------------------------------------
    # Common invocation path with all guards
    # ------------------------------------------------------------------

    def _invoke(
        self,
        *,
        task: LLMTask,
        state: GraphState,
        user_message_builder,
    ) -> Any | None:
        """Returns the validated Pydantic instance on success, or None
        when any guard short-circuits (caller falls back to deterministic).
        Always populates `self.last_call_trace` so orchestration nodes
        can attach the per-call telemetry to GraphState.
        """
        tool_name, tool_description, directive, schema_cls = _TASK_DESCRIPTORS[task]

        # Compute prompt hashes from the cacheable prefix (system +
        # tool schema). Done up-front so every exit branch can attach
        # the same hashes to the trace.
        system_blocks = self._system_blocks_for(directive)
        user_message = user_message_builder(state)
        tool_schema = _stable_tool_schema(schema_cls)
        prompt_hash = _hash_prompt_prefix(
            system_blocks, tool_name, tool_description, tool_schema
        )

        # 1. Budget hard-block check (read-only snapshot — no consume yet).
        snap = self._budget.snapshot()
        if snap.hard_block:
            logger.warning(
                "llm_backend.budget_hard_block",
                extra={
                    "task": task,
                    "consumed_usd": snap.consumed_usd,
                    "budget_usd": snap.budget_usd,
                },
            )
            self.last_call_trace = self._fallback_trace(
                task=task,
                prompt_hash=prompt_hash,
                fallback_reason="budget_hard_block",
            )
            return None

        # 2. Circuit breaker permission. If OPEN, this raises CircuitOpen.
        try:
            self._breaker.acquire()
        except CircuitOpen as exc:
            logger.warning(
                "llm_backend.circuit_open",
                extra={"task": task, "reason": str(exc)},
            )
            self.last_call_trace = self._fallback_trace(
                task=task,
                prompt_hash=prompt_hash,
                fallback_reason="circuit_open",
            )
            return None

        # 3. Provider invocation.
        started = time.monotonic()
        try:
            result = self._provider.call_with_tool(
                system=system_blocks,
                user_message=user_message,
                tool_name=tool_name,
                tool_description=tool_description,
                tool_input_schema=tool_schema,
                max_tokens=self._max_tokens,
            )
        except ProviderError as exc:
            latency_s = time.monotonic() - started
            self._breaker.record_failure(latency_s)
            logger.warning(
                "llm_backend.provider_error",
                extra={
                    "task": task,
                    "kind": exc.kind,
                    "retryable": exc.retryable,
                    "request_id": exc.request_id,
                    "status_code": exc.status_code,
                },
            )
            self.last_call_trace = self._fallback_trace(
                task=task,
                prompt_hash=prompt_hash,
                fallback_reason=exc.kind,
                request_id=exc.request_id,
                latency_ms=int(latency_s * 1000),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            latency_s = time.monotonic() - started
            self._breaker.record_failure(latency_s)
            logger.warning(
                "llm_backend.unexpected_error",
                extra={"task": task, "error_type": type(exc).__name__, "error": str(exc)},
            )
            self.last_call_trace = self._fallback_trace(
                task=task,
                prompt_hash=prompt_hash,
                fallback_reason="unknown",
                latency_ms=int(latency_s * 1000),
            )
            return None

        # 4. Record success on the breaker (latency tracked).
        self._breaker.record_success(result.latency_s or (time.monotonic() - started))

        # 5. Consume budget AFTER the call so we charge actual usage.
        usage = TokenUsage(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_input_tokens=result.usage.cache_read_input_tokens,
            cache_creation_input_tokens=result.usage.cache_creation_input_tokens,
        )
        cost: float = 0.0
        try:
            cost = estimate_cost_usd(result.model_id, usage)
            if cost > 0:
                self._budget.consume(cost)
        except Exception as exc:  # noqa: BLE001
            # Cost-tracking failure must not break the call. Log and
            # continue — budget undercount is acceptable; missing
            # results are not.
            logger.warning(
                "llm_backend.cost_consume_failed",
                extra={"task": task, "error": str(exc)},
            )

        # 6. Pydantic re-validation. The provider has already JSON-
        # validated, but we re-validate here to enforce Literal
        # narrowing and `extra="forbid"` from constraints/specs.py.
        try:
            decision = schema_cls.model_validate(result.arguments)
        except ValidationError as exc:
            logger.warning(
                "llm_backend.schema_validation_failed",
                extra={
                    "task": task,
                    "request_id": result.request_id,
                    "errors": [str(e) for e in exc.errors()],
                    "arguments": dict(result.arguments),
                },
            )
            self.last_call_trace = self._success_trace_then_fallback(
                task=task,
                provider_name=self._provider.provider_name,
                result=result,
                prompt_hash=prompt_hash,
                cost_usd=cost,
                fallback_reason="validation_error",
            )
            return None

        # Success path — record the successful call trace.
        self.last_call_trace = LLMCallTrace(
            task=task,
            provider=self._provider.provider_name,
            model_id=result.model_id,
            request_id=result.request_id,
            prompt_hash=prompt_hash,
            tool_call_hash=_hash_tool_call(tool_name, dict(result.arguments)),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            latency_ms=int(result.latency_s * 1000),
            cost_usd_estimate=cost,
            stop_reason=result.stop_reason,
            skill_md_version=self._skill_md_version,
            fallback_to_deterministic=False,
            fallback_reason=None,
        )
        return decision

    # ------------------------------------------------------------------
    # Trace-building helpers — keep the _invoke control flow readable.
    # ------------------------------------------------------------------

    def _fallback_trace(
        self,
        *,
        task: LLMTask,
        prompt_hash: str,
        fallback_reason: str,
        request_id: Optional[str] = None,
        latency_ms: int = 0,
    ) -> LLMCallTrace:
        """Build a trace for a call that fell through to the
        deterministic backend before producing a successful provider
        response. No token usage / cost — those columns stay zero."""
        return LLMCallTrace(
            task=task,
            provider=self._provider.provider_name,
            model_id="",
            request_id=request_id,
            prompt_hash=prompt_hash,
            tool_call_hash="",
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            latency_ms=latency_ms,
            cost_usd_estimate=0.0,
            stop_reason=None,
            skill_md_version=self._skill_md_version,
            fallback_to_deterministic=True,
            fallback_reason=fallback_reason,
        )

    def _success_trace_then_fallback(
        self,
        *,
        task: LLMTask,
        provider_name: str,
        result,
        prompt_hash: str,
        cost_usd: float,
        fallback_reason: str,
    ) -> LLMCallTrace:
        """Build a trace for the case where the provider RETURNED
        successfully but Pydantic re-validation rejected the
        arguments. We DID burn provider tokens, so the trace records
        them — but `fallback_to_deterministic=True` because the
        graph still served the deterministic answer."""
        return LLMCallTrace(
            task=task,
            provider=provider_name,
            model_id=result.model_id,
            request_id=result.request_id,
            prompt_hash=prompt_hash,
            tool_call_hash="",
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_input_tokens=result.usage.cache_read_input_tokens,
            cache_creation_input_tokens=result.usage.cache_creation_input_tokens,
            latency_ms=int((result.latency_s or 0) * 1000),
            cost_usd_estimate=cost_usd,
            stop_reason=result.stop_reason,
            skill_md_version=self._skill_md_version,
            fallback_to_deterministic=True,
            fallback_reason=fallback_reason,
        )

    # ------------------------------------------------------------------
    # Cacheable system-prompt assembly. Two SystemBlocks both marked
    # cache.enabled=True so providers that support prompt caching place
    # breakpoints correctly. Block 1 is the verbatim skill catalog
    # (~5k tokens, stable). Block 2 is the per-task directive (small,
    # also stable per task).
    # ------------------------------------------------------------------

    def _system_blocks_for(self, directive: str) -> list[SystemBlock]:
        return [
            SystemBlock(
                text=(
                    "# ASOE Skills Reference (read-only catalog)\n\n"
                    f"{self._skill_catalog}"
                ),
                cache=CacheControl(enabled=True),
            ),
            SystemBlock(
                text=(
                    f"{UNTRUSTED_DATA_PREAMBLE}\n\n"
                    f"{directive}"
                ),
                cache=CacheControl(enabled=True),
            ),
        ]


# ---------------------------------------------------------------------------
# User-message builders. Pure functions — no module-level state, no
# datetime/uuid interpolation, no random ordering. Keys are sorted
# explicitly so two calls with structurally-identical events produce
# byte-identical output.
# ---------------------------------------------------------------------------


def _safe_event_view(state: GraphState) -> dict[str, Any]:
    """Coerce OrderEvent into a deterministic, scalar-only view."""
    e = state.event
    return {
        "credit_limit": e.credit_limit,
        "current_exposure": e.current_exposure,
        "event_type": e.event_type,
        "line_count": e.line_count,
        "line_item": e.line_item,
        "order_id": e.order_id,
        "po_price": e.po_price,
        "requester_role": e.requester_role,
        "retailer_id": e.retailer_id,
        "sap_base_price": e.sap_base_price,
        "sku": e.sku,
    }


def _build_intent_user_message(state: GraphState) -> str:
    event_view = _safe_event_view(state)
    metadata = sanitize_metadata_for_llm(state.event.metadata)
    lines = [
        "# Event",
        *[f"  {k}: {event_view[k]!r}" for k in sorted(event_view)],
        "",
        render_untrusted_block(metadata),
        "",
        "Call the `classify_intent` tool with your decision.",
    ]
    return "\n".join(lines)


def _build_recipe_user_message(state: GraphState) -> str:
    intent_value = state.intent.value if state.intent is not None else "UNKNOWN"
    event_view = _safe_event_view(state)
    metadata = sanitize_metadata_for_llm(state.event.metadata)
    lines = [
        "# Classified Intent",
        f"  intent: {intent_value!r}",
        "",
        "# Event",
        *[f"  {k}: {event_view[k]!r}" for k in sorted(event_view)],
        "",
        render_untrusted_block(metadata),
        "",
        "Call the `propose_recipe` tool with the recipe filename.",
    ]
    return "\n".join(lines)


def _build_shadow_user_message(state: GraphState) -> str:
    intent_value = state.intent.value if state.intent is not None else "UNKNOWN"
    event_view = _safe_event_view(state)
    metadata = sanitize_metadata_for_llm(state.event.metadata)
    lines = [
        "# Proposed Action Under Audit",
        f"  intent: {intent_value!r}",
        f"  selected_recipe: {state.selected_recipe!r}",
        f"  batch_total_variance: {state.batch_total_variance!r}",
        f"  line_count: {state.event.line_count!r}",
        "",
        "# Event",
        *[f"  {k}: {event_view[k]!r}" for k in sorted(event_view)],
        "",
        render_untrusted_block(metadata),
        "",
        "Call the `shadow_decision` tool with GREEN / YELLOW / RED, "
        "reasons, and policy_hits.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool-schema serialisation: deterministic key order.
# ---------------------------------------------------------------------------


def _stable_tool_schema(schema_cls: type) -> dict[str, Any]:
    """Return the JSON schema for `schema_cls` with deterministic key
    order at every level. Pydantic's model_json_schema() already
    produces stable output on a single Python version, but we sort
    explicitly so prompt-cache hits compose across pods running
    different Python builds.
    """
    return _sort_keys(schema_cls.model_json_schema())


def _sort_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sort_keys(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_sort_keys(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Hashing helpers — feed the LLMCallTrace prompt_hash / tool_call_hash
# fields. SHA-256 hex digests so two pods comparing audit logs see the
# same value when their inputs were identical, and never see prompt
# content in the audit trail (only hashes).
# ---------------------------------------------------------------------------


def _hash_prompt_prefix(
    system_blocks: list[SystemBlock],
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
) -> str:
    """Hash the cacheable prefix the provider sees: system blocks
    (text + cache flags) + tool definition + tool schema. Two calls
    that hash to the same value should hit prompt cache; the
    invalidator audit (tests/test_llm_cache_invalidators.py) asserts
    the inverse."""
    h = hashlib.sha256()
    for block in system_blocks:
        h.update(block.text.encode("utf-8"))
        h.update(b"\x1f")  # separator
        h.update(b"1" if block.cache.enabled else b"0")
        h.update(b"\x1f")
        h.update((block.cache.ttl or "").encode("utf-8"))
        h.update(b"\x1e")  # block separator
    h.update(tool_name.encode("utf-8"))
    h.update(b"\x1f")
    h.update(tool_description.encode("utf-8"))
    h.update(b"\x1f")
    h.update(json.dumps(tool_schema, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _hash_tool_call(tool_name: str, arguments: dict[str, Any]) -> str:
    """Hash the tool call (name + canonicalised arguments). Stable
    across runs with identical decisions; lets the audit trail
    spot-check repeated outcomes without storing argument PII."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256()
    h.update(tool_name.encode("utf-8"))
    h.update(b"\x1f")
    h.update(canonical.encode("utf-8"))
    return h.hexdigest()


__all__ = (
    "RemoteLLMBackend",
)
