"""ADR-039 — L2 LLM Compliance Shadow second-opinion primitive.

This module ships the **observe-only X.1** surface: a single
constrained-output L2 inference, a per-tenant 24-hour cache, the
gating triggers (financial-impact ≥ $500 OR deterministic-YELLOW),
and SLI counters. The L4 harness wire-up
(`orchestration/nodes.py::shadow_audit`) is **deliberately not
shipped here** — that is Thread 4 / H.5 work and lands together
with the harness extensions.

Per ADR-039 §6.1 X.1 ships:
  * `compliance/shadow_llm.py` (this file)
  * `knowledge/shadow_llm/` bundle
  * `LLMCallTrace.task='shadow_llm'` extension
  * `ComplianceDecision.llm_shadow_verdict` field
  * SLI metrics
  * Cache infrastructure

What is **not** in scope for X.1 (and therefore not in this
module):
  * Affecting the final compliance verdict (X.2+ decision).
  * Choosing a real model (procurement gate per ADR-039 §8.1).
  * Running the L2 call against deterministic-RED records (the
    harness short-circuits per ADR-039 §4.1; the primitive
    enforces the same invariant defensively).

Everything here is **deterministic by construction** when the
provider is the stub: no I/O, no clock dependency for the
verdict (clock is only stamped on the trace metadata), no
cross-tenant cache leakage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable

import yaml

from contracts.models import ComplianceDecision, ShadowLLMVerdict, ShadowStatus

logger = logging.getLogger("asoe.compliance.shadow_llm")

# ---------------------------------------------------------------------------
# Bundle loading — the L0 knowledge surface
# ---------------------------------------------------------------------------

SHADOW_LLM_BUNDLE_DIR = Path("knowledge/shadow_llm")


@dataclass(frozen=True)
class ShadowLLMBundle:
    """Loaded L0 shadow_llm bundle (system prompt + vocabulary +
    rollout config). Held immutably; rotation = reload, not mutate."""

    bundle_version: str
    system_prompt: str
    concerns_vocabulary: tuple[str, ...]
    rollout_phase: str
    financial_impact_threshold_usd: Optional[float]
    invocation_financial_floor_usd: float
    extended_cross_check_enabled: bool
    cache_ttl_seconds: int
    inference_temperature: float
    wall_clock_timeout_ms: int
    model_id_alias: str


def _read_yaml(path: Path) -> Mapping[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_bundle(bundle_dir: Path = SHADOW_LLM_BUNDLE_DIR) -> ShadowLLMBundle:
    """Read the on-disk bundle and return an immutable snapshot.

    Raises FileNotFoundError when the bundle is missing — the X.1
    cutover requires the operator to ship the bundle alongside the
    primitive; we don't fall back to defaults silently.
    """
    metadata = _read_yaml(bundle_dir / "metadata.yaml")
    rollout = metadata.get("rollout") or {}
    inference = metadata.get("inference") or {}

    vocab_path = bundle_dir / "concerns_vocabulary.yaml"
    vocab_data = _read_yaml(vocab_path)
    concerns = tuple(
        str(entry["id"])
        for entry in vocab_data.get("concerns", [])
        if entry.get("id")
    )

    system_prompt = (bundle_dir / "system_prompt.md").read_text(encoding="utf-8")

    return ShadowLLMBundle(
        bundle_version=str(metadata.get("bundle_version", "0.0.0")),
        system_prompt=system_prompt,
        concerns_vocabulary=concerns,
        rollout_phase=str(rollout.get("current_phase", "X.1")),
        financial_impact_threshold_usd=(
            float(rollout["financial_impact_threshold_usd"])
            if rollout.get("financial_impact_threshold_usd") is not None
            else None
        ),
        invocation_financial_floor_usd=float(
            rollout.get("invocation_financial_floor_usd", 500.0),
        ),
        extended_cross_check_enabled=bool(
            rollout.get("extended_cross_check_enabled", False),
        ),
        cache_ttl_seconds=int(rollout.get("cache_ttl_seconds", 86_400)),
        inference_temperature=float(inference.get("temperature", 0.0)),
        wall_clock_timeout_ms=int(inference.get("wall_clock_timeout_ms", 2000)),
        model_id_alias=str(inference.get("model_id_alias", "shadow-llm-default")),
    )


# ---------------------------------------------------------------------------
# Provider Protocol + Stub
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowLLMRequest:
    """Inputs to one L2 inference. Hashable via JSON serialisation
    so the cache key is stable across processes."""

    intent: str
    recipe_name: str
    recipe_params: Mapping[str, Any]
    proposed_action: str
    deterministic_status: str
    deterministic_reasons: tuple[str, ...]
    deterministic_policy_hits: tuple[str, ...]
    case_context_summary: Optional[str]
    customer_profile: Mapping[str, Any]


@runtime_checkable
class LLMShadowProvider(Protocol):
    """Provider boundary for L2 Shadow inference. Real providers
    (Anthropic, local Ollama, hybrid) implement this; tests use
    `StubLLMShadowProvider`."""

    def evaluate(
        self,
        request: ShadowLLMRequest,
        *,
        bundle: ShadowLLMBundle,
        timeout_ms: int,
    ) -> ShadowLLMVerdict:
        ...


class StubLLMShadowProvider:
    """Deterministic stand-in for a real LLM provider.

    The X.1 surface ships ahead of the procurement decision
    (ADR-039 §8.1). The stub returns reproducible verdicts so the
    primitive is testable end-to-end. Behaviour:

      * If `deterministic_status == "YELLOW"` → ``AGREE`` with the
        deterministic reasons echoed back. (The L2 confirms the L1
        gate; reviewer still sees YELLOW.)
      * If a recipe param literally contains the string
        ``"force_disagree"`` (test escape hatch) → ``DISAGREE_DOWNGRADE``
        with a vocabulary entry.
      * Otherwise → ``ABSTAIN`` with confidence 0.5.

    This bias matches the system prompt's "ABSTAIN when in doubt"
    discipline (ADR-039 §3.2). Real providers replace this entirely.
    """

    def __init__(self, *, model_id: str = "stub-llm-shadow-v1") -> None:
        self.model_id = model_id

    def evaluate(
        self,
        request: ShadowLLMRequest,
        *,
        bundle: ShadowLLMBundle,
        timeout_ms: int,
    ) -> ShadowLLMVerdict:
        params_blob = json.dumps(dict(request.recipe_params), sort_keys=True)
        if "force_disagree" in params_blob:
            return ShadowLLMVerdict(
                action="DISAGREE_DOWNGRADE",
                reason="Stub provider triggered by `force_disagree` token in recipe params.",
                confidence=0.9,
                policy_concerns=[bundle.concerns_vocabulary[0]] if bundle.concerns_vocabulary else [],
                bundle_version=bundle.bundle_version,
                model_id=self.model_id,
            )
        if request.deterministic_status == "YELLOW":
            reason = (
                "Concur with deterministic YELLOW: "
                + (request.deterministic_reasons[0] if request.deterministic_reasons else "rules indicated review.")
            )
            return ShadowLLMVerdict(
                action="AGREE",
                reason=reason[:200],
                confidence=0.7,
                policy_concerns=[],
                bundle_version=bundle.bundle_version,
                model_id=self.model_id,
            )
        return ShadowLLMVerdict(
            action="ABSTAIN",
            reason="Stub provider: insufficient context to take a position.",
            confidence=0.5,
            policy_concerns=[],
            bundle_version=bundle.bundle_version,
            model_id=self.model_id,
        )


# ---------------------------------------------------------------------------
# Cache — per-tenant, ADR-039 §5.5
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    verdict: ShadowLLMVerdict
    expires_at: float  # epoch seconds


class ShadowLLMCache:
    """Per-tenant 24-hour cache for L2 Shadow verdicts.

    Cache key = SHA-256 of `(tenant_id, bundle_version, model_id,
    canonical(request))`. Tenant inclusion is mandatory per
    ADR-038 §5.8 / ADR-039 §5.5 — never serve a Tenant-A entry to
    a Tenant-B request even when the rest of the inputs match.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def make_key(
        *,
        tenant_id: str,
        bundle_version: str,
        model_id: str,
        request: ShadowLLMRequest,
    ) -> str:
        canonical = json.dumps(
            {
                "tenant": tenant_id,
                "bundle": bundle_version,
                "model": model_id,
                "intent": request.intent,
                "recipe": request.recipe_name,
                "params": dict(request.recipe_params),
                "action": request.proposed_action,
                "det_status": request.deterministic_status,
                "det_reasons": list(request.deterministic_reasons),
                "det_hits": list(request.deterministic_policy_hits),
                "case": request.case_context_summary or "",
                "customer": dict(request.customer_profile),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str, *, now: Optional[float] = None) -> Optional[ShadowLLMVerdict]:
        now = now if now is not None else time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                return None
            # Re-stamp cache_hit on the returned copy so the
            # caller's audit trail is honest.
            return entry.verdict.model_copy(update={"cache_hit": True, "latency_ms": 0, "cost_usd_estimate": 0.0})

    def put(
        self,
        key: str,
        verdict: ShadowLLMVerdict,
        *,
        ttl_seconds: int,
        now: Optional[float] = None,
    ) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._entries[key] = _CacheEntry(
                verdict=verdict.model_copy(update={"cache_hit": False}),
                expires_at=now + ttl_seconds,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Module-level cache (singleton). Tests reset via `cache.clear()`.
shadow_llm_cache = ShadowLLMCache()


# ---------------------------------------------------------------------------
# SLI counters — ADR-039 §7.3
# ---------------------------------------------------------------------------


@dataclass
class ShadowLLMMetrics:
    """In-process counters mirroring the ADR-039 §7.3 Prometheus
    surface. The Phase X.1 dashboard scrapes these via the existing
    `/api/v1/health/metrics` endpoint pattern (wire-up lands with
    the harness in Thread 4).
    """

    # Invocation counters (gating triggers)
    invocations_total: int = 0
    invocations_by_trigger: Dict[str, int] = field(default_factory=dict)
    cache_hits_total: int = 0

    # Verdict counters (action distribution)
    verdicts_by_action: Dict[str, int] = field(default_factory=dict)

    # Failure-mode counters
    timeouts_total: int = 0
    unavailable_total: int = 0
    validation_errors_total: int = 0

    # Latency (rolling sum + count for p99 alerting; full histogram
    # ships when the Prometheus client lib is wired in Thread 4).
    latency_ms_sum: int = 0
    latency_ms_count: int = 0

    # Cost
    cost_usd_total: float = 0.0

    # Skip counters (gated-out events)
    skipped_red_total: int = 0
    skipped_below_floor_total: int = 0

    def reset(self) -> None:
        self.invocations_total = 0
        self.invocations_by_trigger.clear()
        self.cache_hits_total = 0
        self.verdicts_by_action.clear()
        self.timeouts_total = 0
        self.unavailable_total = 0
        self.validation_errors_total = 0
        self.latency_ms_sum = 0
        self.latency_ms_count = 0
        self.cost_usd_total = 0.0
        self.skipped_red_total = 0
        self.skipped_below_floor_total = 0

    def disagreement_rate(self) -> float:
        """ADR-039 §7.3 — DISAGREE_DOWNGRADE / total invocations."""
        if self.invocations_total == 0:
            return 0.0
        downgrades = self.verdicts_by_action.get("DISAGREE_DOWNGRADE", 0)
        return downgrades / self.invocations_total

    def abstain_rate(self) -> float:
        if self.invocations_total == 0:
            return 0.0
        return self.verdicts_by_action.get("ABSTAIN", 0) / self.invocations_total

    def cache_hit_rate(self) -> float:
        if self.invocations_total == 0:
            return 0.0
        return self.cache_hits_total / self.invocations_total

    def avg_latency_ms(self) -> float:
        if self.latency_ms_count == 0:
            return 0.0
        return self.latency_ms_sum / self.latency_ms_count


shadow_llm_metrics = ShadowLLMMetrics()


# ---------------------------------------------------------------------------
# The L2 Shadow primitive
# ---------------------------------------------------------------------------


@dataclass
class ShadowLLMOutcome:
    """What the harness gets back from ``ShadowLLM.evaluate(...)``.

    On a successful invocation, ``verdict`` is populated and the
    harness writes it to ``ComplianceDecision.llm_shadow_verdict``.
    On a fall-through (gating skipped, RED short-circuit, provider
    failure), ``verdict`` is None and ``skip_reason`` carries the
    audit-bearing token. Either way the SLI counters are updated.
    """

    verdict: Optional[ShadowLLMVerdict]
    skip_reason: Optional[str] = None
    invocation_trigger: Optional[str] = None
    cache_hit: bool = False


# Trigger constants (mirror ADR-039 §5.2; bundle metadata may
# override the floor at config-time, but these are the defaults).
TRIGGER_FINANCIAL_IMPACT = "financial_impact"
TRIGGER_DETERMINISTIC_YELLOW = "deterministic_yellow"
SKIP_DETERMINISTIC_RED = "deterministic_red_short_circuit"
SKIP_BELOW_FLOOR = "below_invocation_floor"
SKIP_PROVIDER_TIMEOUT = "provider_timeout"
SKIP_PROVIDER_UNAVAILABLE = "provider_unavailable"
SKIP_VALIDATION_ERROR = "provider_validation_error"


class ShadowLLM:
    """L2 LLM Compliance Shadow second opinion (ADR-039 §3.2).

    Constructed once per process with a provider + bundle + cache;
    `evaluate(...)` is called per event. Holds no per-event state.
    """

    def __init__(
        self,
        *,
        provider: LLMShadowProvider,
        bundle: Optional[ShadowLLMBundle] = None,
        cache: Optional[ShadowLLMCache] = None,
        metrics: Optional[ShadowLLMMetrics] = None,
    ) -> None:
        self.provider = provider
        self.bundle = bundle if bundle is not None else load_bundle()
        self.cache = cache if cache is not None else shadow_llm_cache
        self.metrics = metrics if metrics is not None else shadow_llm_metrics

    # ----- gating ---------------------------------------------------------

    def should_invoke(
        self,
        *,
        deterministic: ComplianceDecision,
        financial_impact_usd: float,
    ) -> tuple[bool, Optional[str]]:
        """ADR-039 §5.2 gating. Returns ``(should, reason_token)``.

        ``reason_token`` is the trigger name on a positive answer
        and the skip token on a negative one. The harness logs
        whichever is returned.
        """
        if deterministic.status == ShadowStatus.RED:
            return False, SKIP_DETERMINISTIC_RED
        if deterministic.status == ShadowStatus.YELLOW:
            return True, TRIGGER_DETERMINISTIC_YELLOW
        if financial_impact_usd >= self.bundle.invocation_financial_floor_usd:
            return True, TRIGGER_FINANCIAL_IMPACT
        return False, SKIP_BELOW_FLOOR

    # ----- invocation -----------------------------------------------------

    def evaluate(
        self,
        *,
        tenant_id: str,
        request: ShadowLLMRequest,
        deterministic: ComplianceDecision,
        financial_impact_usd: float = 0.0,
    ) -> ShadowLLMOutcome:
        """One L2 evaluation pass: gate → cache → provider →
        record. Pure with respect to inputs; the only side effects
        are SLI counter increments and cache writes."""
        should, token = self.should_invoke(
            deterministic=deterministic,
            financial_impact_usd=financial_impact_usd,
        )
        if not should:
            if token == SKIP_DETERMINISTIC_RED:
                self.metrics.skipped_red_total += 1
            else:
                self.metrics.skipped_below_floor_total += 1
            return ShadowLLMOutcome(verdict=None, skip_reason=token)

        cache_key = ShadowLLMCache.make_key(
            tenant_id=tenant_id,
            bundle_version=self.bundle.bundle_version,
            model_id=getattr(self.provider, "model_id", self.bundle.model_id_alias),
            request=request,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            self.metrics.invocations_total += 1
            self.metrics.invocations_by_trigger[token] = (
                self.metrics.invocations_by_trigger.get(token, 0) + 1
            )
            self.metrics.cache_hits_total += 1
            self.metrics.verdicts_by_action[cached.action] = (
                self.metrics.verdicts_by_action.get(cached.action, 0) + 1
            )
            return ShadowLLMOutcome(
                verdict=cached, invocation_trigger=token, cache_hit=True,
            )

        # Cold path — call the provider with the wall-clock budget.
        t0 = time.perf_counter()
        try:
            verdict = self.provider.evaluate(
                request,
                bundle=self.bundle,
                timeout_ms=self.bundle.wall_clock_timeout_ms,
            )
        except TimeoutError:
            self.metrics.timeouts_total += 1
            return ShadowLLMOutcome(verdict=None, skip_reason=SKIP_PROVIDER_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — provider boundary
            # Constrained-generation rejections raise ValueError /
            # ValidationError; everything else is unavailability.
            if isinstance(exc, (ValueError,)):
                logger.warning("shadow_llm validation_error: %s", exc)
                self.metrics.validation_errors_total += 1
                return ShadowLLMOutcome(
                    verdict=None, skip_reason=SKIP_VALIDATION_ERROR,
                )
            logger.warning("shadow_llm unavailable: %s", exc)
            self.metrics.unavailable_total += 1
            return ShadowLLMOutcome(verdict=None, skip_reason=SKIP_PROVIDER_UNAVAILABLE)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        verdict = verdict.model_copy(
            update={
                "latency_ms": latency_ms,
                "bundle_version": verdict.bundle_version or self.bundle.bundle_version,
                "cache_hit": False,
            },
        )

        # Validate that named concerns are in the closed vocabulary.
        # An out-of-vocab concern is a constrained-generation defect;
        # we drop it (the audit log records the discard via the SLI)
        # rather than silently propagate.
        if self.bundle.concerns_vocabulary and verdict.policy_concerns:
            cleaned = [c for c in verdict.policy_concerns if c in self.bundle.concerns_vocabulary]
            if cleaned != list(verdict.policy_concerns):
                self.metrics.validation_errors_total += 1
                verdict = verdict.model_copy(update={"policy_concerns": cleaned})

        # Counters
        self.metrics.invocations_total += 1
        self.metrics.invocations_by_trigger[token] = (
            self.metrics.invocations_by_trigger.get(token, 0) + 1
        )
        self.metrics.verdicts_by_action[verdict.action] = (
            self.metrics.verdicts_by_action.get(verdict.action, 0) + 1
        )
        self.metrics.latency_ms_sum += latency_ms
        self.metrics.latency_ms_count += 1
        self.metrics.cost_usd_total += verdict.cost_usd_estimate

        # Cache write — observed verdicts only (not skip outcomes).
        self.cache.put(cache_key, verdict, ttl_seconds=self.bundle.cache_ttl_seconds)

        return ShadowLLMOutcome(
            verdict=verdict, invocation_trigger=token, cache_hit=False,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Trace-stamp helper. Module-private to keep callers from
    importing datetime directly."""
    return datetime.now(timezone.utc).isoformat()
