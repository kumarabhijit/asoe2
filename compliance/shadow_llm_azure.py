"""ADR-039 §8.1 — Azure OpenAI–backed L2 LLM Shadow provider.

Procurement decision (post-merge, 2026-05-09): Azure OpenAI for the
L2 LLM Shadow second opinion. The deployment alias and exact model
id are environment-configured; this module does not pin a specific
GPT version. Operators set:

  AZURE_OPENAI_ENDPOINT        = https://<resource>.openai.azure.com
  AZURE_OPENAI_API_KEY         = <key>
  AZURE_OPENAI_API_VERSION     = e.g. 2024-10-21
  AZURE_OPENAI_SHADOW_DEPLOYMENT = <deployment-name>

The provider implements the `LLMShadowProvider` Protocol from
`compliance.shadow_llm`. Failures translate to the same fallthrough
modes the StubLLMShadowProvider exhibits in tests
(TimeoutError → SKIP_PROVIDER_TIMEOUT; ValueError → SKIP_VALIDATION_ERROR;
anything else → SKIP_PROVIDER_UNAVAILABLE).

The constrained-output schema is enforced via Azure OpenAI's
`response_format={"type": "json_schema", ...}` parameter — same
mechanism Anthropic / Google / Ollama paths use elsewhere in this
codebase. ADR-039 §3.2 invariant: the schema deliberately omits
any `DISAGREE_UPGRADE` action; the LLM cannot return one.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from compliance.shadow_llm import ShadowLLMBundle, ShadowLLMRequest
from contracts.models import ShadowLLMVerdict

logger = logging.getLogger("asoe.compliance.shadow_llm_azure")


# ---------------------------------------------------------------------------
# Constrained-output JSON schema
# ---------------------------------------------------------------------------

# The schema mirrors `ShadowLLMVerdict` field-for-field. Azure OpenAI's
# JSON-schema response format requires `additionalProperties: false`
# and `required: [...]` to actually enforce; both are set here.
SHADOW_VERDICT_JSON_SCHEMA: Dict[str, Any] = {
    "name": "shadow_llm_verdict",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "reason", "confidence", "policy_concerns"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"],
                "description": (
                    "ADR-039 §3.2 closed action vocabulary. No "
                    "`DISAGREE_UPGRADE` — asymmetric authority is "
                    "structural in the schema."
                ),
            },
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": (
                    "One-sentence rationale, ≤200 chars. Surfaced "
                    "verbatim to the human reviewer when L2 downgrades."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "policy_concerns": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Named concerns from `concerns_vocabulary.yaml`; "
                    "out-of-vocab entries are dropped post-call."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Configuration — env-driven, no hardcoded model id
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureShadowConfig:
    endpoint: str
    api_key: str
    api_version: str
    deployment: str

    @classmethod
    def from_env(cls) -> "AzureShadowConfig":
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
        api_key = os.environ["AZURE_OPENAI_API_KEY"]
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
        deployment = os.environ["AZURE_OPENAI_SHADOW_DEPLOYMENT"]
        return cls(
            endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            deployment=deployment,
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AzureOpenAIShadowProvider:
    """`LLMShadowProvider` Protocol impl backed by Azure OpenAI.

    Holds a singleton `openai.AzureOpenAI` client; `evaluate()` is
    thread-safe under the SDK's HTTP/2 pool. Wall-clock timeout is
    honoured via the SDK's `timeout` parameter (whole-call cap, not
    per-byte).
    """

    def __init__(
        self,
        config: Optional[AzureShadowConfig] = None,
        *,
        sdk_client: Any = None,
    ) -> None:
        self._config = config if config is not None else AzureShadowConfig.from_env()
        self._client = sdk_client if sdk_client is not None else self._build_client()
        # `model_id` is the field the harness reads onto the
        # `LLMCallTrace` and `ShadowLLMVerdict.model_id`. We expose
        # the *deployment name* as the model id because Azure
        # routes via deployment, not the underlying model alias.
        self.model_id = self._config.deployment

    def _build_client(self) -> Any:
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — packaging issue
            raise RuntimeError(
                "Azure OpenAI shadow provider requires the `openai` "
                "package. Install with: pip install 'asoe[openai]'.",
            ) from exc
        return openai.AzureOpenAI(
            api_key=self._config.api_key,
            api_version=self._config.api_version,
            azure_endpoint=self._config.endpoint,
        )

    # ----- LLMShadowProvider Protocol -----------------------------------

    def evaluate(
        self,
        request: ShadowLLMRequest,
        *,
        bundle: ShadowLLMBundle,
        timeout_ms: int,
    ) -> ShadowLLMVerdict:
        """One Azure OpenAI chat completion. Constrained output via
        `response_format=json_schema`; replayability via temperature 0.

        Failure-mode mapping (caller in
        `compliance.shadow_llm.ShadowLLM.evaluate` translates these):
          * `TimeoutError` → SKIP_PROVIDER_TIMEOUT
          * `ValueError` (constrained-generation defect, including
            JSON-decode / schema-mismatch) → SKIP_VALIDATION_ERROR
          * Anything else → SKIP_PROVIDER_UNAVAILABLE
        """
        user_message = self._format_user_message(request)
        timeout_s = max(0.001, timeout_ms / 1000.0)

        started = time.perf_counter()
        try:
            response = self._client.chat.completions.create(
                model=self._config.deployment,
                messages=[
                    {"role": "system", "content": bundle.system_prompt},
                    {"role": "user", "content": user_message},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": SHADOW_VERDICT_JSON_SCHEMA,
                },
                temperature=bundle.inference_temperature,
                max_tokens=512,
                timeout=timeout_s,
            )
        except TimeoutError:
            raise
        except Exception as exc:
            # Azure OpenAI's `APIStatusError` family carries
            # `status_code`; we coarsely classify in the upstream
            # caller. Re-raise so it lands in the unavailable bucket
            # unless it's a clear schema-mismatch (400).
            status = getattr(exc, "status_code", None)
            if status == 400:
                raise ValueError(str(exc)) from exc
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._parse_response(
            response, bundle=bundle, latency_ms=latency_ms,
        )

    # ----- helpers ------------------------------------------------------

    @staticmethod
    def _format_user_message(request: ShadowLLMRequest) -> str:
        """Single canonical serialisation so byte-identical inputs
        produce byte-identical prompts (replayability)."""
        body = {
            "intent": request.intent,
            "recipe_name": request.recipe_name,
            "recipe_params": dict(request.recipe_params),
            "proposed_action": request.proposed_action,
            "deterministic_verdict": {
                "status": request.deterministic_status,
                "reasons": list(request.deterministic_reasons),
                "policy_hits": list(request.deterministic_policy_hits),
            },
            "case_context_summary": request.case_context_summary or "",
            "customer_profile": dict(request.customer_profile),
        }
        return json.dumps(body, sort_keys=True, ensure_ascii=False)

    def _parse_response(
        self,
        response: Any,
        *,
        bundle: ShadowLLMBundle,
        latency_ms: int,
    ) -> ShadowLLMVerdict:
        try:
            choice = response.choices[0]
            content = choice.message.content
        except (AttributeError, IndexError) as exc:
            raise ValueError(
                f"Azure OpenAI response missing choices/content: {exc}",
            ) from exc

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Azure OpenAI response not valid JSON: {exc}",
            ) from exc

        # Pydantic enforces the schema on this side too — schema
        # match in the request × pydantic validation × out-of-vocab
        # filter in `compliance.shadow_llm` = three layers of defence.
        # `model_id` and `bundle_version` are stamped here so the
        # caller sees them on the verdict.
        request_id = (
            getattr(response, "id", None)
            or getattr(response, "request_id", None)
            or str(uuid.uuid4())
        )
        cost_estimate = self._estimate_cost(response)
        return ShadowLLMVerdict(
            action=parsed["action"],
            reason=parsed["reason"],
            confidence=float(parsed["confidence"]),
            policy_concerns=list(parsed.get("policy_concerns") or []),
            bundle_version=bundle.bundle_version,
            model_id=self.model_id,
            request_id=request_id,
            cache_hit=False,
            latency_ms=latency_ms,
            cost_usd_estimate=cost_estimate,
        )

    @staticmethod
    def _estimate_cost(response: Any) -> float:
        """Best-effort cost estimate from the response's `usage`
        block. Azure OpenAI bills per-token; the rate depends on the
        deployment's underlying model. We don't pin pricing here —
        ops sets `ASOE_AZURE_OPENAI_COST_USD_PER_1K_TOKENS_*` for
        reporting if needed. Default returns 0.0 so the SLI counter
        works without crashing, and Step 7's k8s manifest can wire
        per-deployment pricing when the procurement quote lands."""
        try:
            input_rate = float(
                os.getenv("ASOE_AZURE_OPENAI_INPUT_USD_PER_1K", "0") or "0",
            )
            output_rate = float(
                os.getenv("ASOE_AZURE_OPENAI_OUTPUT_USD_PER_1K", "0") or "0",
            )
        except ValueError:
            return 0.0
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0.0
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        return (
            (prompt_tokens / 1000.0) * input_rate
            + (completion_tokens / 1000.0) * output_rate
        )


# ---------------------------------------------------------------------------
# Factory — selects between Stub and Azure based on env
# ---------------------------------------------------------------------------


def select_shadow_provider() -> Any:
    """Pick the L2 Shadow provider based on env. When
    `AZURE_OPENAI_SHADOW_DEPLOYMENT` is set, the Azure provider is
    constructed; otherwise the stub stays as the default."""
    if os.getenv("AZURE_OPENAI_SHADOW_DEPLOYMENT", "").strip():
        return AzureOpenAIShadowProvider()
    from compliance.shadow_llm import StubLLMShadowProvider
    return StubLLMShadowProvider()
