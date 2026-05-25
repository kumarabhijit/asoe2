from __future__ import annotations

# Policy configuration — externalized business thresholds
#
# Centralises every tunable threshold that was previously hardcoded in
# recipes and orchestration utilities.  The values may later be loaded
# from a config file, environment, or policy service; for now they are
# module-level constants that serve as the single source of truth.
#
# CLAUDE.md §1: recipes execute, skills guide; thresholds live here.

# ---------------------------------------------------------------------------
# Price Adjustment thresholds
# ---------------------------------------------------------------------------

MAX_DISCOUNT_ALLOWED: float = 0.15
"""Maximum discount (as a decimal fraction) before a price correction is rejected."""

PRICE_CONDITION_TYPE: str = "YK07"
"""SAP condition type applied for customer-match price corrections."""

# ---------------------------------------------------------------------------
# Credit Hold Release thresholds
# ---------------------------------------------------------------------------

CREDIT_AUTHORIZED_ROLES: tuple[str, ...] = ("ORDER_MANAGER", "FINANCE_DIRECTOR")
"""Roles permitted to auto-release a credit hold."""

CREDIT_EXPOSURE_TOLERANCE: float = 5_000.0
"""Maximum (exposure − limit) delta before requiring manual Finance review."""

# ---------------------------------------------------------------------------
# Duplicate PO Detection thresholds
# ---------------------------------------------------------------------------

DUPLICATE_PO_THRESHOLD_AUTO_BLOCK: float = 0.90
"""Composite score at or above which a PO is auto-blocked."""

DUPLICATE_PO_THRESHOLD_REVIEW_REQUIRED: float = 0.70
"""Composite score at or above which a PO requires manual review."""

DUPLICATE_PO_THRESHOLD_SOFT_FLAG: float = 0.50
"""Composite score at or above which a PO is soft-flagged."""

# Autonomy level per resolution action (spec §3.3) — autonomy vocab v2
# (ADR-042 §5; contracts/autonomy.py): L1 = auto / no human review (most
# autonomous), L2 = execute & notify, L3 = prepare & await approval, L4 =
# escalate to human (least autonomous). Migrated from v1 (the mirror ladder)
# behaviour-preservingly: each action keeps its v1 degree of automation, only
# the naming flips (v1 L1↔v2 L4, v1 L2↔v2 L3, …). See
# tests/test_autonomy_ladder_v2_migration.py.
DUPLICATE_PO_AUTONOMY_LEVELS: dict[str, str] = {
    "BLOCK_AND_NOTIFY": "L2",
    "MERGE": "L3",
    "SUPERSEDE": "L3",
    "ALLOW_BOTH": "L2",
    "ESCALATE": "L4",
    "REQUEST_BUYER_CONFIRMATION": "L3",
}
"""Maps resolution action → autonomy level (vocab v2) for routing decisions."""

# ---------------------------------------------------------------------------
# Mass-update / line-count threshold
# ---------------------------------------------------------------------------

MASS_UPDATE_LINE_COUNT_THRESHOLD: int = 10
"""Line count above which an event is classified as a mass update risk."""

# ---------------------------------------------------------------------------
# Circuit breaker thresholds (orchestration/utils.py)
# ---------------------------------------------------------------------------

CIRCUIT_BREAKER_MAX_UPDATES: int = 50
"""Maximum pricing updates allowed per 5-minute window."""

CIRCUIT_BREAKER_MAX_VARIANCE: float = 10_000.0
"""Maximum total dollar variance per batch before halting."""

# ---------------------------------------------------------------------------
# Re-analysis (human-triggered graph replay) thresholds
# ---------------------------------------------------------------------------

REANALYSIS_MAX_ATTEMPTS: int = 3
"""Maximum number of human-initiated re-analyses per exception.

Bounded to prevent outcome-shopping — repeatedly re-running the graph in
hopes of a different Compliance Shadow verdict. The prior outcome is always
preserved in the exception's reanalysis_history for audit.
"""

# ---------------------------------------------------------------------------
# Discrepancy threshold
# ---------------------------------------------------------------------------

DISCREPANCY_THRESHOLD: float = 0.15
"""Maximum price discrepancy (%) before flagging as outside threshold."""

# ---------------------------------------------------------------------------
# Four-eyes high-value override (Phase 2 #5)
# ---------------------------------------------------------------------------

HIGH_VALUE_OVERRIDE_THRESHOLD_USD: float = 10_000.0
"""Financial impact at/above which a manager override requires a second
reviewer to cosign before the action is applied.

When the exception's financial_impact_usd meets or exceeds this threshold,
POST /exceptions/{id}/override transitions the record to PENDING_COSIGN
instead of RESOLVED; a different manager+ must then POST to
/exceptions/{id}/override/cosign to approve (applies the action) or reject
(restores prior lifecycle). Standard SOX control under §404 — any single
manager cannot unilaterally authorize a material change."""

# ---------------------------------------------------------------------------
# Price Hold Release thresholds
# ---------------------------------------------------------------------------

PRICE_HOLD_TOLERANCE_PCT: float = 0.02
"""Absolute variance (decimal fraction) at or below which a held order may
be auto-released without review. Variance is |po_price − sap_base_price| /
sap_base_price."""

PRICE_HOLD_HARD_BLOCK_PCT: float = 0.10
"""Absolute variance above which a held order is hard-blocked (REJECTED)
rather than escalated to manual review. Between tolerance and hard-block,
the order is escalated (MANUAL_REVIEW_REQUIRED)."""

# ---------------------------------------------------------------------------
# EDI Mismatch autonomy levels (per sub_type)
# ---------------------------------------------------------------------------

EDI_MISMATCH_AUTONOMY_LEVELS: dict[str, str] = {
    "SKU_MISMATCH": "L2",
    "QTY_MISMATCH": "L3",
    "UOM_MISMATCH": "L3",
    "SHIP_TO_MISMATCH": "L4",
}
"""Maps EDI 850 line-mismatch sub_type → autonomy level (vocab v2, ADR-042 §5):
L1 = auto, L2 = execute & notify, L3 = prepare & await approval, L4 = escalate
to human. Migrated behaviour-preservingly from v1 (SKU L3→L2, QTY/UOM L2→L3,
SHIP_TO L1→L4 — same degree of automation, mirror naming). PRICE_MISMATCH is
absent by design — those events are routed to CONTRACTUAL_CORRECTION /
PriceAdjustmentRecipe.py at classifier time and never reach this recipe."""

# ---------------------------------------------------------------------------
# Back-Order (OOS) thresholds
# Per prototype spec SD-OOS-001 (<50% gap) and SD-OOS-002 (>=50% gap).
# ---------------------------------------------------------------------------

BACK_ORDER_SEVERE_GAP_PCT: float = 0.50
"""Gap percentage at or above which a back-order is classified SEVERE.
SD-OOS-002 rule. Below this, the recipe can recommend split-shipment or
alternate-DC fulfilment; at/above, escalation to a buyer decision is
mandatory."""

# ---------------------------------------------------------------------------
# Over-Max quantity thresholds
# Per prototype spec SD-OM-001 (exceeds contract) and SD-OM-002 (>50%).
# ---------------------------------------------------------------------------

OVER_MAX_SEVERE_EXCEEDANCE_PCT: float = 0.50
"""Exceedance percentage above contract max at/above which an order is
SEVERE. SD-OM-002 rule. Below this, automated trim-to-max is permitted;
at/above, sales-manager review is required before any trim action."""

# ---------------------------------------------------------------------------
# Minimum Order Quantity thresholds
# Per prototype spec SD-MOQ-001 (<25% shortfall) and SD-MOQ-002 (>=25%).
# ---------------------------------------------------------------------------

MOQ_SEVERE_SHORTFALL_PCT: float = 0.25
"""Shortfall percentage at/above which a MOQ shortfall is SEVERE.
SD-MOQ-002 rule. Below this, automated round-up to MOQ is permitted;
at/above, the order needs a KNMT waiver or sales-manager escalation."""

MOQ_UPLIFT_REVIEW_PCT: float = 0.10
"""Round-up uplift at/above which operator sign-off is required even if
the underlying shortfall is below SEVERE. Prevents silent large-value
upsizes from slipping through the automated path."""

# ---------------------------------------------------------------------------
# Pallet configuration thresholds
# Per prototype specs SD-PLT-001 (broken layer) and SD-PLT-002 (partial).
# ---------------------------------------------------------------------------

PALLET_CONFIG_MIN_FILL_PCT: float = 0.90
"""Pallet fill percentage below which the recipe flags a partial-pallet
violation (SD-PLT-002). Used by PalletAlignmentRecipe to decide whether
to suggest round-down to full layers or accept the partial pallet."""

PALLET_CONFIG_BROKEN_LAYER_FILL_PCT: float = 1.00
"""Fill percentage above which a row is considered to have a broken
layer overage (SD-PLT-001 — ordered qty spans partial layer(s)). Only
round-down alignment is safe above this ratio."""

# ---------------------------------------------------------------------------
# Delivery delay thresholds
# Per prototype specs SD-DELAY-001 (2-4 days) and SD-DELAY-002 (>=5 days).
# ---------------------------------------------------------------------------

DELIVERY_DELAY_MINOR_DAYS: int = 2
"""Minimum days-late threshold for a MINOR delay (SD-DELAY-001).
Below this the recipe short-circuits with no action needed."""

DELIVERY_DELAY_SEVERE_DAYS: int = 5
"""Days-late threshold at/above which a delivery delay is classified
SEVERE (SD-DELAY-002). Below this the recipe can recommend expedite /
split-ship; at/above, reschedule-to-later-window plus buyer notification
is required."""

# ---------------------------------------------------------------------------
# Email Order Entry thresholds (ADR-034)
# Per `knowledge/skills/manual-order-intake/specs/order_entry_spec.md` §3 (L2 Behavior
# Thresholds) and §4 (Resolution Workflows / AUTO_CORRECT confidence floor).
# ---------------------------------------------------------------------------

EMAIL_ORDER_AUTO_APPROVE_CONFIDENCE: float = 0.95
"""Composite confidence at or above which a clean (no-validation-failure)
email-channel order envelope is one-click approve eligible. Closed lower
bound."""

EMAIL_ORDER_REVIEW_BAND_LOW: float = 0.85
"""Closed lower bound of the standard-review band. Below this the record
is classified LOW_CONFIDENCE."""

EMAIL_ORDER_AUTO_CORRECT_CONFIDENCE: float = 0.99
"""Composite confidence at or above which a deterministic AUTO_CORRECT
fix may be applied — but only when every validation failure is in the
auto-correctable allowlist (UOM normalisation, ship-to format, PO-number
padding). Spec §4 explicitly ties AUTO_CORRECT to confidence ≥ 0.99."""

# Autonomy level per resolution action (spec §5) — autonomy vocab v2
# (ADR-042 §5): L1 = auto / no human review (most autonomous), L2 = execute &
# notify, L3 = prepare & await approval, L4 = escalate to human (least
# autonomous). Migrated behaviour-preservingly from v1 (mirror naming). Per
# ADR-034 §3.5 no action graduates to the most-autonomous tier (v2 L1) until
# the calibration loop (ADR-032) ships graduation signals — so the most
# autonomous entries here remain at v2 L2 (execute & notify), the v2 image of
# the v1 "act & inform" (L3) tier.
MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS: dict[str, str] = {
    "ONE_CLICK_APPROVE":     "L2",
    "STANDARD_REVIEW":       "L3",
    "LOW_CONFIDENCE_FLAG":   "L4",
    "AUTO_CORRECT":          "L2",
    "REQUEST_CLARIFICATION": "L3",
    "ESCALATE":              "L4",
    "REJECT":                "L4",
}
"""Maps MANUAL_ORDER_INTAKE recommended_action → autonomy level (vocab v2).
ONE_CLICK_APPROVE / AUTO_CORRECT sit at v2 L2 (execute & notify), not the most
autonomous v2 L1 — pending ADR-032 calibration graduation, no action runs with
no human notification."""

# Legacy name retained as an alias for the back-compat window
# defined in ADR-034 §6.2 (transitional until 2026-08-12). New
# code imports MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS; this binding
# is removed in the same PR that flips the §6.2 hard rejection on.
EMAIL_ORDER_ENTRY_AUTONOMY_LEVELS = MANUAL_ORDER_INTAKE_AUTONOMY_LEVELS

# ---------------------------------------------------------------------------
# LLM provider routing & cost guardrails
# ---------------------------------------------------------------------------
# V1 flips a single deterministic fallback path into a pluggable per-task
# routing layer. Operators choose a provider per task (intent / recipe /
# shadow) via env vars; the router resolves to a backend and a daily USD
# budget bounds spend. Production defaults to `fallback` until a tenant or
# the operator explicitly opts in. SOX / Compliance Shadow integrity is
# preserved by defaulting `shadow` to deterministic regardless of provider
# choice (see ASOE_LLM_DISABLE_FOR).

from typing import Literal

LLMProvider = Literal[
    "anthropic",    # Anthropic API flavor — direct (api.anthropic.com)
                    # OR via Azure AI Foundry private endpoint (set
                    # ANTHROPIC_BASE_URL to the Foundry URL).
    "openai",       # OpenAI API flavor — OpenAI direct, Azure OpenAI
                    # (set OPENAI_BASE_URL + OPENAI_API_VERSION),
                    # OR any OpenAI-compatible endpoint such as a
                    # self-hosted vLLM / TGI cluster running an HF
                    # model (e.g. Qwen) — point OPENAI_BASE_URL at
                    # the cluster URL and use the model id as
                    # OPENAI_MODEL.
    "google",       # Google Vertex AI / Gemini.
    "ollama",       # Ollama (cloud or self-hosted, OpenAI-compatible).
    "huggingface",  # HuggingFace Inference Endpoints / HF Inference
                    # API. For Qwen / Llama / Mistral / any HF model
                    # served via HF's hosted inference. Self-hosted
                    # vLLM/TGI is reachable via the `openai` provider
                    # instead — that path doesn't need an HF auth
                    # token and uses the OpenAI Python SDK.
    "outlines",     # Local constrained generation (Outlines + HF
                    # transformers, in-process).
    "local",        # Sandbox SLM via LOCAL_LLM_BACKEND_CLASS.
    "fallback",     # DeterministicFallbackBackend — always-available net.
]
"""Allowed values for ASOE_LLM_PROVIDER and per-task overrides
(ASOE_LLM_PROVIDER_INTENT / _RECIPE / _SHADOW). The router rejects
any other value and falls closed to `fallback`.

Provider key = API flavor. The cloud / hosting choice is config
(base_url, deployment_name, api_version) — there is no separate
'azure_<provider>' enum value. Anthropic on Foundry =
provider=anthropic + base_url=<foundry-url>. Azure OpenAI =
provider=openai + base_url=<azure-openai-url> + api_version=<...>.
Qwen on a private vLLM cluster = provider=openai +
base_url=<vllm-url>. Qwen on HF Inference Endpoints =
provider=huggingface. This keeps the enum stable as new clouds
appear."""

LLMTask = Literal["intent", "recipe", "shadow"]
"""The three trio methods served by a constraint backend. Each is
independently routable so an operator can pin shadow to deterministic
while letting intent and recipe go through a remote LLM."""

LLM_PROVIDER_DEFAULT: LLMProvider = "fallback"
"""Default provider when ASOE_LLM_PROVIDER is unset. Production deploys
must keep this default and opt in explicitly per task."""

LLM_DEFAULT_MODEL_ID: str = "claude-sonnet-4-6"
"""Default Anthropic model when ANTHROPIC_MODEL is unset. Aligned with
architecture_v3.md §4.2 (Reasoning Core: Claude 4.6 Sonnet)."""

LLM_DAILY_USD_BUDGET_DEFAULT: float = 5.0
"""Daily spend cap (USD) when ASOE_LLM_DAILY_USD_BUDGET is unset.
At/above this value the router hard-blocks the LLM tier and serves
the deterministic fallback. Sized for V1 sandbox shakeout; raise via
env var for GA."""

LLM_BUDGET_HARD_BLOCK_PCT: float = 1.0
"""Daily budget consumption fraction at/above which all LLM calls are
short-circuited to the deterministic fallback. Default 100%."""

LLM_BUDGET_SOFT_WARN_PCT: float = 0.8
"""Daily budget consumption fraction at/above which a Prometheus alert
fires (no behavior change). Default 80%."""

LLM_PER_RUN_USD_CAP: float = 0.10
"""Maximum USD spend allowed across the trio of calls in a single
graph run. Pathological-prompt guard; rejects the run to FAIL_TO_HUMAN
if exceeded mid-run."""

LLM_CIRCUIT_BREAKER_ERROR_RATE_PCT: float = 0.25
"""Rolling 60-second LLM error rate at/above which the LLM-tier
circuit breaker trips, routing all calls to the deterministic
fallback for a 5-minute cooldown. Separate from the $10k batch
breaker in orchestration/utils.py."""

LLM_CIRCUIT_BREAKER_P95_LATENCY_S: float = 15.0
"""Rolling 60-second p95 latency (seconds) above which the LLM-tier
circuit breaker trips. Pairs with the error-rate threshold above."""

LLM_CIRCUIT_BREAKER_COOLDOWN_S: int = 300
"""Time the LLM-tier circuit breaker stays in OPEN state before
moving to HALF_OPEN. Default 5 minutes."""

# ---------------------------------------------------------------------------
# Gateway-tier circuit breaker (DoR #8 — email/SAP/infra gateway parity with
# the LLM tier). Applied per gateway_name at the GatewayExecutor chokepoint.
# ---------------------------------------------------------------------------

GATEWAY_CIRCUIT_BREAKER_ERROR_RATE_PCT: float = 0.50
"""Rolling 60-second per-gateway error rate at/above which the gateway-tier
circuit breaker trips, short-circuiting further calls to that gateway to an
UNAVAILABLE response for a cooldown window. Higher than the LLM tier (0.25):
gateway reads are retried/fanned-out and a transient infra blip shouldn't trip
as eagerly as a model outage."""

GATEWAY_CIRCUIT_BREAKER_P95_LATENCY_S: float = 10.0
"""Rolling 60-second per-gateway p95 latency (seconds) above which the
gateway-tier breaker trips. Pairs with the error-rate threshold above."""

GATEWAY_CIRCUIT_BREAKER_COOLDOWN_S: int = 60
"""Time the gateway-tier breaker stays OPEN before probing HALF_OPEN. Shorter
than the LLM tier (60s vs 5m): infra gateways recover faster than a rate-limited
model and the read is non-binding evidence enrichment."""

# ---------------------------------------------------------------------------
# Attachment-fetch SSRF allowlist (DoR #10). Hosts the attachment_fetch gateway
# is permitted to retrieve email attachments from. Allowlist-first: an inbound
# email cannot point an attachment at an arbitrary (internal) URL. Override per
# tenant/deploy as the real attachment stores are wired.
# ---------------------------------------------------------------------------

ATTACHMENT_FETCH_ALLOWED_HOSTS: frozenset = frozenset({
    "attachments.acme.example",
    "mail.acme.example",
    "s3.amazonaws.com",
})
"""Hostnames (exact or dot-suffix) the attachment_fetch gateway may fetch from.
Everything else — and any host resolving to a non-global address — is refused by
`hardening.ssrf.validate_outbound_url`."""

ATTACHMENT_MAX_BYTES: int = 25 * 1024 * 1024
"""Maximum size (bytes) of a single attachment the store will persist. Inbound
attachments above this are rejected at ingestion (`AttachmentTooLarge`) rather
than stored — a DoS / oversize guard on the email-intake path. 25 MiB matches a
typical mailbox per-message attachment ceiling; raise per deploy if needed."""

LLM_CALL_TIMEOUT_S: float = 30.0
"""Per-call timeout for an Anthropic SDK request (constrained tool-use
output is normally <2s; tail latency on cache writes can hit 10s+).
At 3 calls × 3 SDK retries × 30s = 270s worst case, comfortably under
the 8-min p50 SLA."""

LLM_CROSS_CHECK_DISAGREEMENT_REASON: str = "LLM_DETERMINISTIC_DISAGREEMENT"
"""ExecutionLog reason emitted when the remote LLM and the
deterministic classifier produce different intents. The graph routes
to MANUAL_REVIEW_REQUIRED — a valid terminal state per CLAUDE.md §5
that defers a contested classification to a human."""

# Pricing table (USD per 1M tokens) — used by the budget tracker to
# convert observed token counts into USD spend. Cached snapshot from
# 2026-04-15; keep in sync with shared/models.md when new models land.
LLM_PRICING_USD_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write_5m": 1.25},
}
"""USD per 1M tokens by model and token kind. cache_read ≈ 0.1× input;
cache_write_5m ≈ 1.25× input (5-minute TTL). 1-hour TTL is 2× input
and added when we evaluate it (V1.x). Used at trace-emission time to
populate TraceRecord.cost_usd_estimate."""
