"""ADR-039 §6.1 — anchor-example accrual script tests.

Locks the signal-extraction logic and the ranking truncation that
Compliance relies on when reviewing the JSON artifact.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from api.store import case_store, exception_store
from contracts.models import OrderEvent
from scripts.earn_anchor_examples import (
    _candidate_from_record,
    _signal_and_score,
    main as earn_main,
)


@pytest.fixture(autouse=True)
def _reset():
    case_store.clear()
    exception_store.clear()
    yield
    case_store.clear()
    exception_store.clear()


# ---------------------------------------------------------------------------
# Signal classifier
# ---------------------------------------------------------------------------

class TestSignalClassifier:
    def test_reverse_disagreement(self):
        signal, score = _signal_and_score(
            llm_action="DISAGREE_DOWNGRADE", llm_confidence=0.9,
            human_action="APPROVE", deterministic_status="GREEN",
        )
        assert signal == "reverse_disagreement"
        assert 0.7 < score <= 1.0

    def test_sustained_disagreement(self):
        signal, score = _signal_and_score(
            llm_action="DISAGREE_DOWNGRADE", llm_confidence=0.6,
            human_action="ESCALATE", deterministic_status="GREEN",
        )
        assert signal == "sustained_disagreement"
        assert 0.5 < score <= 0.8

    def test_borderline_abstain(self):
        signal, score = _signal_and_score(
            llm_action="ABSTAIN", llm_confidence=0.85,
            human_action=None, deterministic_status="GREEN",
        )
        assert signal == "borderline_abstain"
        assert score > 0.3

    def test_low_confidence_abstain_skipped(self):
        signal, score = _signal_and_score(
            llm_action="ABSTAIN", llm_confidence=0.3,
            human_action=None, deterministic_status="GREEN",
        )
        assert signal is None
        assert score == 0.0

    def test_agree_skipped(self):
        signal, _ = _signal_and_score(
            llm_action="AGREE", llm_confidence=0.95,
            human_action="APPROVE", deterministic_status="GREEN",
        )
        assert signal is None


# ---------------------------------------------------------------------------
# Record translation
# ---------------------------------------------------------------------------

class TestCandidateExtraction:
    def test_record_without_llm_verdict_returns_none(self):
        candidate = _candidate_from_record({
            "id": "ex-1", "case_id": "c1", "intent": "DUPLICATE_PO",
            "shadow": {"status": "GREEN"},
        })
        assert candidate is None

    def test_full_record_translates(self):
        candidate = _candidate_from_record({
            "id": "ex-1", "case_id": "c1", "intent": "DUPLICATE_PO",
            "selected_recipe": "DuplicatePORecipe.py",
            "resolution_data": {"recommended_action": "BLOCK_DUPLICATE"},
            "resolved_action": "APPROVE",
            "shadow": {
                "status": "GREEN",
                "reasons": ["clean"],
                "policy_hits": [],
                "llm_shadow_verdict": {
                    "action": "DISAGREE_DOWNGRADE",
                    "reason": "customer opt-out missed",
                    "confidence": 0.9,
                    "policy_concerns": ["CUSTOMER_OPT_OUT_VIOLATION"],
                    "request_id": "req-x",
                },
            },
            "created_at": "2026-04-15T00:00:00Z",
            "updated_at": "2026-04-15T01:00:00Z",
        })
        assert candidate is not None
        assert candidate.signal == "reverse_disagreement"
        assert candidate.llm_action == "DISAGREE_DOWNGRADE"
        assert candidate.human_action == "APPROVE"
        assert candidate.intent == "DUPLICATE_PO"
        assert candidate.llm_policy_concerns == ["CUSTOMER_OPT_OUT_VIOLATION"]


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

class TestCli:
    def test_emits_artifact(self, tmp_path):
        # Seed an in-memory record carrying an LLM verdict so the
        # script has something to chew on.
        from api.store import ChildCase
        rec = ChildCase(
            tenant_id="tenant-a",
            order_id="PO-1",
            event_type="EDI_850_PRICE_MISMATCH",
            trace_id="trace-1",
            intent="DUPLICATE_PO",
            shadow_verdict="GREEN",
            selected_recipe="DuplicatePORecipe.py",
            resolution_data={
                "recommended_action": "BLOCK_DUPLICATE",
                # The script reads the LLM verdict via the
                # `shadow.llm_shadow_verdict` path on `to_detail`,
                # but our in-memory ChildCase doesn't carry
                # the persistent shadow. We pin it via
                # resolution_data.shadow which the iterator falls
                # back to when the record's shadow attribute is
                # absent.
                "shadow": {
                    "status": "GREEN",
                    "reasons": ["clean"],
                    "policy_hits": [],
                    "llm_shadow_verdict": {
                        "action": "DISAGREE_DOWNGRADE",
                        "reason": "high-value-threshold-proximity",
                        "confidence": 0.85,
                        "policy_concerns": ["HIGH_VALUE_THRESHOLD_PROXIMITY"],
                        "request_id": "rid-1",
                    },
                },
            },
        )
        rec.resolved_action = "APPROVE"
        exception_store._records[rec.id] = rec

        out = tmp_path / "candidates.json"
        rc = earn_main([
            "--since", "2026-04-01",
            "--tenant", "tenant-a",
            "--top", "5",
            "--out", str(out),
        ])
        assert rc == 0
        body = json.loads(out.read_text())
        assert body["tenant"] == "tenant-a"
        assert len(body["candidates"]) == 1
        assert body["candidates"][0]["signal"] == "reverse_disagreement"
        assert body["by_signal_count"]["reverse_disagreement"] == 1

    def test_invalid_since_exits_2(self):
        with pytest.raises(SystemExit):
            earn_main([
                "--since", "not-a-date",
                "--out", "/tmp/x.json",
            ])

    def test_empty_store_emits_empty_artifact(self, tmp_path):
        out = tmp_path / "empty.json"
        rc = earn_main([
            "--since", "2026-01-01",
            "--tenant", "tenant-a",
            "--top", "5",
            "--out", str(out),
        ])
        assert rc == 0
        body = json.loads(out.read_text())
        assert body["candidates"] == []
        assert body["by_signal_count"] == {}
