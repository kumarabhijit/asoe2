"""Step 7 — Azure provider tests (B1 + B2).

Verifies the procurement wire-up shipped post-merge:
  * `AzureOpenAIShadowProvider` (ADR-039 §8.1) — parses Azure
    OpenAI chat-completion responses into `ShadowLLMVerdict`,
    handles 400 (constrained-generation defect) → ValueError so
    the upstream caller routes to SKIP_VALIDATION_ERROR.
  * `AzureDocumentIntelligenceProvider` (ADR-038 §11.2) — projects
    DI's key_value_pairs + documents.fields into
    `List[ExtractedField]`.
  * `ChandraOCRProvider` — translates the JSON output of the
    open-source fallback.
  * Factory selection from env vars.

Tests inject mock SDK clients; no Azure / HuggingFace round-trip.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List

import pytest

from agents.primitives.extract_attachment import AttachmentRef, ExtractedField
from agents.primitives.extract_providers import (
    AzureDIConfig,
    AzureDocumentIntelligenceProvider,
    ChandraOCRProvider,
    StubMultimodalProvider,
    select_multimodal_provider,
)
from compliance.shadow_llm import ShadowLLMRequest, load_bundle
from compliance.shadow_llm_azure import (
    AzureOpenAIShadowProvider,
    AzureShadowConfig,
    select_shadow_provider,
)


# ---------------------------------------------------------------------------
# Azure OpenAI shadow provider
# ---------------------------------------------------------------------------


@dataclass
class _MockUsage:
    prompt_tokens: int = 100
    completion_tokens: int = 50


@dataclass
class _MockMessage:
    content: str


@dataclass
class _MockChoice:
    message: _MockMessage


@dataclass
class _MockChatCompletion:
    id: str
    choices: List[_MockChoice]
    usage: _MockUsage


class _MockAzureOpenAIClient:
    """Minimal stand-in for `openai.AzureOpenAI` — drops the SDK's
    transport while preserving the call surface the provider hits."""

    def __init__(self, response: _MockChatCompletion = None, raise_exc: Exception = None):
        self._response = response
        self._raise_exc = raise_exc
        self.last_kwargs: Dict[str, Any] = {}
        self.chat = self  # SDK exposes `client.chat.completions.create`
        self.completions = self

    def create(self, **kwargs: Any) -> _MockChatCompletion:
        self.last_kwargs = kwargs
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._response


@pytest.fixture
def shadow_request() -> ShadowLLMRequest:
    return ShadowLLMRequest(
        intent="DUPLICATE_PO",
        recipe_name="DuplicatePORecipe",
        recipe_params={"po_number": "PO-1"},
        proposed_action="BLOCK_DUPLICATE",
        deterministic_status="GREEN",
        deterministic_reasons=(),
        deterministic_policy_hits=(),
        case_context_summary=None,
        customer_profile={"tier": "Strategic"},
    )


@pytest.fixture
def bundle():
    return load_bundle()


@pytest.fixture
def fake_config():
    return AzureShadowConfig(
        endpoint="https://test.openai.azure.com",
        api_key="test-key",
        api_version="2024-10-21",
        deployment="shadow-deployment",
    )


def _make_response(action: str = "AGREE", reason: str = "ok",
                   confidence: float = 0.8,
                   policy_concerns: list[str] | None = None) -> _MockChatCompletion:
    payload = json.dumps({
        "action": action,
        "reason": reason,
        "confidence": confidence,
        "policy_concerns": policy_concerns or [],
    })
    return _MockChatCompletion(
        id="resp-123",
        choices=[_MockChoice(message=_MockMessage(content=payload))],
        usage=_MockUsage(),
    )


class TestAzureOpenAIShadowProvider:
    def test_evaluate_returns_verdict(self, shadow_request, bundle, fake_config):
        client = _MockAzureOpenAIClient(response=_make_response("AGREE", "ok"))
        provider = AzureOpenAIShadowProvider(
            config=fake_config, sdk_client=client,
        )
        verdict = provider.evaluate(shadow_request, bundle=bundle, timeout_ms=2000)
        assert verdict.action == "AGREE"
        assert verdict.reason == "ok"
        assert verdict.bundle_version == bundle.bundle_version
        assert verdict.model_id == "shadow-deployment"
        assert verdict.request_id == "resp-123"
        assert verdict.latency_ms >= 0

    def test_evaluate_calls_with_correct_schema(
        self, shadow_request, bundle, fake_config,
    ):
        client = _MockAzureOpenAIClient(response=_make_response("ABSTAIN", "x"))
        AzureOpenAIShadowProvider(
            config=fake_config, sdk_client=client,
        ).evaluate(shadow_request, bundle=bundle, timeout_ms=2000)
        # Schema enforcement happens via response_format.
        rf = client.last_kwargs["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "shadow_llm_verdict"
        # No DISAGREE_UPGRADE in the schema enum (asymmetric authority
        # is structural, ADR-039 §3.2).
        action_enum = rf["json_schema"]["schema"]["properties"]["action"]["enum"]
        assert "DISAGREE_UPGRADE" not in action_enum
        assert set(action_enum) == {"AGREE", "DISAGREE_DOWNGRADE", "ABSTAIN"}

    def test_evaluate_temperature_zero(
        self, shadow_request, bundle, fake_config,
    ):
        client = _MockAzureOpenAIClient(response=_make_response())
        AzureOpenAIShadowProvider(
            config=fake_config, sdk_client=client,
        ).evaluate(shadow_request, bundle=bundle, timeout_ms=2000)
        # Replayability requires temperature 0.
        assert client.last_kwargs["temperature"] == 0.0

    def test_evaluate_400_translates_to_value_error(
        self, shadow_request, bundle, fake_config,
    ):
        class _StatusErr(Exception):
            status_code = 400

        client = _MockAzureOpenAIClient(raise_exc=_StatusErr("schema mismatch"))
        provider = AzureOpenAIShadowProvider(
            config=fake_config, sdk_client=client,
        )
        with pytest.raises(ValueError):
            provider.evaluate(shadow_request, bundle=bundle, timeout_ms=2000)

    def test_evaluate_timeout_propagates(
        self, shadow_request, bundle, fake_config,
    ):
        client = _MockAzureOpenAIClient(raise_exc=TimeoutError("slow"))
        provider = AzureOpenAIShadowProvider(
            config=fake_config, sdk_client=client,
        )
        with pytest.raises(TimeoutError):
            provider.evaluate(shadow_request, bundle=bundle, timeout_ms=2000)

    def test_evaluate_unknown_5xx_propagates(
        self, shadow_request, bundle, fake_config,
    ):
        class _StatusErr(Exception):
            status_code = 503

        client = _MockAzureOpenAIClient(raise_exc=_StatusErr("upstream down"))
        provider = AzureOpenAIShadowProvider(
            config=fake_config, sdk_client=client,
        )
        with pytest.raises(_StatusErr):
            provider.evaluate(shadow_request, bundle=bundle, timeout_ms=2000)

    def test_invalid_json_translates_to_value_error(
        self, shadow_request, bundle, fake_config,
    ):
        bad = _MockChatCompletion(
            id="resp-bad",
            choices=[_MockChoice(message=_MockMessage(content="not json"))],
            usage=_MockUsage(),
        )
        client = _MockAzureOpenAIClient(response=bad)
        provider = AzureOpenAIShadowProvider(
            config=fake_config, sdk_client=client,
        )
        with pytest.raises(ValueError):
            provider.evaluate(shadow_request, bundle=bundle, timeout_ms=2000)


class TestSelectShadowProvider:
    def test_default_returns_stub(self, monkeypatch):
        monkeypatch.delenv("AZURE_OPENAI_SHADOW_DEPLOYMENT", raising=False)
        from compliance.shadow_llm import StubLLMShadowProvider
        provider = select_shadow_provider()
        assert isinstance(provider, StubLLMShadowProvider)

    def test_env_set_returns_azure(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
        monkeypatch.setenv("AZURE_OPENAI_SHADOW_DEPLOYMENT", "shadow-dep")
        # Block the real SDK build; we only care that the factory
        # routes here.
        called: dict[str, bool] = {}

        def _no_build(self):
            called["built"] = True
            return None

        monkeypatch.setattr(
            AzureOpenAIShadowProvider, "_build_client", _no_build,
        )
        provider = select_shadow_provider()
        assert isinstance(provider, AzureOpenAIShadowProvider)
        assert called.get("built") is True


# ---------------------------------------------------------------------------
# Azure Document Intelligence
# ---------------------------------------------------------------------------


@dataclass
class _MockDIKey:
    content: str


@dataclass
class _MockDIValue:
    content: str


@dataclass
class _MockDIKeyValuePair:
    key: _MockDIKey
    value: _MockDIValue
    confidence: float


@dataclass
class _MockDIResult:
    key_value_pairs: List[_MockDIKeyValuePair]
    documents: List[Any] = None


class _MockDIPoller:
    def __init__(self, result: _MockDIResult):
        self._result = result

    def result(self) -> _MockDIResult:
        return self._result


class _MockDIClient:
    def __init__(self, result: _MockDIResult = None, raise_exc: Exception = None):
        self._result = result
        self._raise_exc = raise_exc
        self.last_kwargs: Dict[str, Any] = {}

    def begin_analyze_document(self, **kwargs: Any) -> _MockDIPoller:
        self.last_kwargs = kwargs
        if self._raise_exc is not None:
            raise self._raise_exc
        return _MockDIPoller(self._result)


@pytest.fixture
def attachment() -> AttachmentRef:
    return AttachmentRef(
        attachment_id="att-1",
        tenant_id="tenant-a",
        name="po.pdf",
        mime_type="application/pdf",
        bytes=12345,
        template_fingerprint="fp-test",
        bytes_locator="https://example.com/po.pdf",
    )


@pytest.fixture
def di_config():
    return AzureDIConfig(
        endpoint="https://di.example.com",
        api_key="test-key",
        model_id="prebuilt-document",
    )


class TestAzureDocumentIntelligenceProvider:
    def test_extract_translates_kv_pairs(self, attachment, di_config):
        result = _MockDIResult(key_value_pairs=[
            _MockDIKeyValuePair(
                key=_MockDIKey(content="customer_po_number"),
                value=_MockDIValue(content="PO-123"),
                confidence=0.95,
            ),
            _MockDIKeyValuePair(
                key=_MockDIKey(content="total_amount"),
                value=_MockDIValue(content="$1,234.56"),
                confidence=0.91,
            ),
        ])
        client = _MockDIClient(result=result)
        provider = AzureDocumentIntelligenceProvider(
            config=di_config, sdk_client=client,
        )
        fields = provider.extract(attachment)
        assert len(fields) == 2
        assert fields[0].name == "customer_po_number"
        assert fields[0].value == "PO-123"
        assert fields[0].confidence == 0.95

    def test_extract_filters_by_fields_hint(self, attachment, di_config):
        result = _MockDIResult(key_value_pairs=[
            _MockDIKeyValuePair(
                key=_MockDIKey(content="customer_po_number"),
                value=_MockDIValue(content="PO-1"),
                confidence=0.9,
            ),
            _MockDIKeyValuePair(
                key=_MockDIKey(content="ignore_me"),
                value=_MockDIValue(content="xx"),
                confidence=0.9,
            ),
        ])
        client = _MockDIClient(result=result)
        provider = AzureDocumentIntelligenceProvider(
            config=di_config, sdk_client=client,
        )
        fields = provider.extract(
            attachment, fields_hint=["customer_po_number"],
        )
        assert [f.name for f in fields] == ["customer_po_number"]

    def test_extract_400_translates_to_value_error(self, attachment, di_config):
        class _StatusErr(Exception):
            status_code = 400

        client = _MockDIClient(raise_exc=_StatusErr("malformed pdf"))
        provider = AzureDocumentIntelligenceProvider(
            config=di_config, sdk_client=client,
        )
        with pytest.raises(ValueError):
            provider.extract(attachment)

    def test_extract_timeout_propagates(self, attachment, di_config):
        client = _MockDIClient(raise_exc=TimeoutError("slow"))
        provider = AzureDocumentIntelligenceProvider(
            config=di_config, sdk_client=client,
        )
        with pytest.raises(TimeoutError):
            provider.extract(attachment)


# ---------------------------------------------------------------------------
# Chandra OCR (open-source fallback)
# ---------------------------------------------------------------------------


class _MockChandraModel:
    def __init__(self, output: dict):
        self._output = output

    def process(self, *args, **kwargs) -> dict:
        return self._output


class TestChandraOCRProvider:
    def test_extract_translates_json_output(self, attachment):
        model = _MockChandraModel(output={
            "fields": {
                "po_number": {"value": "PO-X", "confidence": 0.88},
                "total": {"value": "1234.56", "confidence": 0.81},
            },
        })
        provider = ChandraOCRProvider(model=model)
        fields = provider.extract(attachment)
        names = {f.name for f in fields}
        assert names == {"po_number", "total"}

    def test_extract_handles_string_blobs(self, attachment):
        # Some Chandra paths return plain strings instead of dicts;
        # we coerce to ExtractedField with a default confidence.
        model = _MockChandraModel(output={
            "fields": {"raw_text": "PO-Y"},
        })
        provider = ChandraOCRProvider(model=model)
        fields = provider.extract(attachment)
        assert fields[0].name == "raw_text"
        assert fields[0].value == "PO-Y"
        assert fields[0].confidence == 0.5

    def test_extract_empty_output_returns_empty_list(self, attachment):
        model = _MockChandraModel(output={})
        provider = ChandraOCRProvider(model=model)
        assert provider.extract(attachment) == []


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


class TestSelectMultimodalProvider:
    def test_default_returns_stub(self, monkeypatch):
        monkeypatch.delenv("ASOE_OCR_PRIMARY", raising=False)
        provider = select_multimodal_provider()
        assert isinstance(provider, StubMultimodalProvider)

    def test_explicit_stub(self, monkeypatch):
        monkeypatch.setenv("ASOE_OCR_PRIMARY", "stub")
        provider = select_multimodal_provider()
        assert isinstance(provider, StubMultimodalProvider)

    def test_azure_di_routing(self, monkeypatch):
        monkeypatch.setenv("ASOE_OCR_PRIMARY", "azure_di")
        monkeypatch.setenv("AZURE_DI_ENDPOINT", "https://di.example.com")
        monkeypatch.setenv("AZURE_DI_API_KEY", "test-key")
        monkeypatch.setattr(
            AzureDocumentIntelligenceProvider, "_build_client",
            lambda self: None,
        )
        provider = select_multimodal_provider()
        assert isinstance(provider, AzureDocumentIntelligenceProvider)

    def test_chandra_routing(self, monkeypatch):
        monkeypatch.setenv("ASOE_OCR_PRIMARY", "chandra")
        provider = select_multimodal_provider()
        assert isinstance(provider, ChandraOCRProvider)
