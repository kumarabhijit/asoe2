"""ADR-039 §6.1 — Anchor-example accrual mechanism.

The X.1 exit criterion requires Compliance to review 30 days of
L2 disagreement traces and earn the first 5–10 anchor examples
before X.2 ratification. The examples are not authored
speculatively; they accrue from real
(L1, L2, human-decision) triples.

This script reads the audit-bearing record store + L2 verdict
log, identifies high-signal disagreement traces, and emits
candidate anchor examples for Compliance review. Operators run:

    python scripts/earn_anchor_examples.py \\
        --since 2026-04-01 --tenant tenant-a \\
        --top 10 --out /tmp/candidates.json

Compliance reviews the JSON, picks the 5–10 to land, and edits
`knowledge/shadow_llm/anchor_examples/<slug>.example.json`. The
metadata.yaml `anchor_examples:` list points at the chosen slugs.

The script does NOT mutate the bundle directly — that's the
Compliance review's prerogative.

Signal ranking (high → low):
  1. **L2 DISAGREE_DOWNGRADE that the human REVERSED** — i.e.,
     the operator approved the original action despite L2's
     downgrade. These are the "LLM cried wolf" cases; including
     them in anchors helps the next L2 inference distinguish.
  2. **L2 DISAGREE_DOWNGRADE that the human SUSTAINED** — i.e.,
     the operator agreed with the LLM. Best-case
     `DISAGREE_DOWNGRADE` examples. Including them helps the L2
     pattern-match similar cases.
  3. **L2 ABSTAIN with high confidence (>=0.7)** — situations
     the L2 nearly-took-a-position on but didn't. Useful as
     borderline-case anchors.

Excluded: AGREE outcomes (the L2 already produced the right
answer; nothing to learn) and low-confidence ABSTAINs (noise).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("asoe.anchor_accrual")


# ---------------------------------------------------------------------------
# Candidate dataclass — what one earned anchor example looks like
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class AnchorCandidate:
    """One candidate anchor example with the signal that earned it.

    Fields mirror the eventual `<slug>.example.json` shape that
    Compliance lands; the script keeps them in sync so
    promotion is a copy operation.
    """

    slug: str
    """Short stable identifier — sha256(case_id + verdict.request_id)[:12]."""
    earned_at: str
    """ISO-8601 timestamp when the source decision was made."""
    signal: str
    """Why this candidate ranks: `reverse_disagreement` (highest),
    `sustained_disagreement`, or `borderline_abstain`."""
    rank_score: float
    """0.0–1.0 — higher = more useful anchor. Drives the `--top`
    truncation."""

    intent: str
    recipe_name: str
    proposed_action: str
    deterministic_status: str
    deterministic_reasons: List[str]
    deterministic_policy_hits: List[str]

    llm_action: str
    llm_reason: str
    llm_confidence: float
    llm_policy_concerns: List[str]

    human_action: Optional[str] = None
    """The reviewer's resolution. None when no human override
    occurred (the verdict stood)."""
    human_reason_code: Optional[str] = None

    notes: str = ""
    """Free-form auditor field — populated by the script with a
    one-line rationale for why this candidate ranks where it does."""


# ---------------------------------------------------------------------------
# Signal-extraction helpers
# ---------------------------------------------------------------------------


def _slug_for(case_id: str, request_id: Optional[str]) -> str:
    seed = f"{case_id}|{request_id or 'no-rid'}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _signal_and_score(
    *,
    llm_action: str,
    llm_confidence: float,
    human_action: Optional[str],
    deterministic_status: str,
) -> tuple[Optional[str], float]:
    """Map the (LLM action, human override) pair to a signal label
    + rank score. Returns (None, 0.0) for outcomes we don't earn
    anchors from (AGREE, low-confidence ABSTAIN)."""
    # Reverse-disagreement: LLM said DOWNGRADE, human approved
    # the original (override action == recommended action). The
    # "LLM cried wolf" anchor.
    if llm_action == "DISAGREE_DOWNGRADE" and human_action in (
        "APPROVE", "approved", "auto_approve",
    ):
        # Higher score for higher LLM confidence — the more sure
        # the LLM was, the more useful the anchor that disproves it.
        return "reverse_disagreement", 0.7 + 0.2 * llm_confidence
    # Sustained-disagreement: LLM said DOWNGRADE, human escalated
    # / overrode toward MANUAL_REVIEW_REQUIRED. The
    # "LLM caught a real one" anchor.
    if llm_action == "DISAGREE_DOWNGRADE" and human_action in (
        "ESCALATE", "MANUAL_REVIEW_REQUIRED", "escalated",
    ):
        return "sustained_disagreement", 0.5 + 0.3 * llm_confidence
    # Borderline ABSTAIN — LLM almost took a position. Useful but
    # ranks below disagreement signals.
    if llm_action == "ABSTAIN" and llm_confidence >= 0.7:
        return "borderline_abstain", 0.3 + 0.1 * llm_confidence
    return None, 0.0


def _candidate_from_record(record: Dict[str, Any]) -> Optional[AnchorCandidate]:
    """Translate one record (with embedded LLM verdict) into a
    candidate. Returns None when the record doesn't carry a useful
    signal."""
    verdict = (record.get("shadow") or {}).get("llm_shadow_verdict")
    if not verdict:
        return None
    llm_action = verdict.get("action") or ""
    llm_confidence = float(verdict.get("confidence") or 0.0)
    human_action = record.get("resolved_action")
    deterministic_status = (record.get("shadow") or {}).get("status") or ""
    signal, score = _signal_and_score(
        llm_action=llm_action,
        llm_confidence=llm_confidence,
        human_action=human_action,
        deterministic_status=deterministic_status,
    )
    if signal is None:
        return None
    notes_map = {
        "reverse_disagreement": (
            "LLM downgraded; human approved the original action — "
            "include to prevent similar false positives."
        ),
        "sustained_disagreement": (
            "LLM downgraded; human concurred — anchor exemplifies "
            "the rule-miss pattern the LLM caught."
        ),
        "borderline_abstain": (
            "LLM nearly took a position (confidence ≥ 0.7) but "
            "abstained — borderline-case anchor."
        ),
    }
    return AnchorCandidate(
        slug=_slug_for(
            record.get("case_id") or record.get("id") or "",
            verdict.get("request_id"),
        ),
        earned_at=record.get("updated_at") or record.get("created_at") or "",
        signal=signal,
        rank_score=score,
        intent=record.get("intent") or "",
        recipe_name=record.get("selected_recipe") or "",
        proposed_action=(
            (record.get("resolution_data") or {}).get("recommended_action") or ""
        ),
        deterministic_status=deterministic_status,
        deterministic_reasons=list(
            (record.get("shadow") or {}).get("reasons") or [],
        ),
        deterministic_policy_hits=list(
            (record.get("shadow") or {}).get("policy_hits") or [],
        ),
        llm_action=llm_action,
        llm_reason=verdict.get("reason") or "",
        llm_confidence=llm_confidence,
        llm_policy_concerns=list(verdict.get("policy_concerns") or []),
        human_action=human_action,
        human_reason_code=record.get("resolution_notes"),
        notes=notes_map[signal],
    )


# ---------------------------------------------------------------------------
# Source iterator — abstracts away in-memory vs DB-backed stores
# ---------------------------------------------------------------------------


def _iter_records_in_window(
    *,
    tenant_id: Optional[str],
    since_iso: str,
) -> List[Dict[str, Any]]:
    """Pull records from the canonical store, filter by tenant +
    time window. The in-memory store is the test path; the
    DB-backed store ships the same shape via DatabaseBackedStore."""
    from api.store import exception_store

    rows: List[Dict[str, Any]] = []
    if tenant_id:
        records, _, _ = exception_store.list(tenant_id=tenant_id, limit=500)
    else:
        # Without tenant filter we walk the in-memory map directly.
        # Production deployments always pass --tenant.
        store_records = getattr(exception_store, "_records", {})
        records = list(store_records.values()) if store_records else []
    for r in records:
        # `exception_store.list` returns ExceptionRecord instances;
        # downstream we want a dict. Use to_detail() when available
        # (the in-memory shape has it), else attribute-walk.
        if hasattr(r, "to_detail"):
            row = r.to_detail().model_dump(mode="json")
        else:
            row = dict(r)
        # Inject the LLM verdict from the live shadow attribute
        # (the persistence model for that field is per-deployment;
        # tests pass it directly on resolution_data).
        row.setdefault("shadow", row.get("resolution_data", {}).get("shadow"))
        if row.get("created_at", "") < since_iso:
            continue
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Earn anchor-example candidates from L2 LLM Shadow "
            "disagreement traces (ADR-039 §6.1 X.1 exit criterion)."
        ),
    )
    parser.add_argument(
        "--since",
        required=True,
        help=(
            "ISO-8601 date (e.g. 2026-04-01). Records older than "
            "this are excluded."
        ),
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help="Tenant id; omit to scan every tenant in the store.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Cap candidates per signal type (default 10).",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output JSON file. Compliance reviews this artifact.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )
    return parser.parse_args(argv)


def _normalise_since(raw: str) -> str:
    """Accept either a date (`2026-04-01`) or an ISO timestamp
    (`2026-04-01T00:00:00+00:00`); always emit a comparable ISO
    string."""
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError as exc:
        raise SystemExit(f"--since must be ISO-8601: {exc}") from exc


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    since_iso = _normalise_since(args.since)
    logger.info(
        "Earning anchor candidates since=%s tenant=%s top=%d",
        since_iso, args.tenant or "(all)", args.top,
    )

    records = _iter_records_in_window(
        tenant_id=args.tenant, since_iso=since_iso,
    )
    logger.info("Scanned %d records in window", len(records))

    candidates: List[AnchorCandidate] = []
    for record in records:
        c = _candidate_from_record(record)
        if c is not None:
            candidates.append(c)

    # Sort by rank_score descending; truncate per signal so we
    # don't flood Compliance with one signal type.
    candidates.sort(key=lambda c: c.rank_score, reverse=True)
    by_signal: Dict[str, List[AnchorCandidate]] = {}
    for c in candidates:
        by_signal.setdefault(c.signal, []).append(c)
    truncated: List[AnchorCandidate] = []
    for sig, group in by_signal.items():
        truncated.extend(group[: args.top])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "since": since_iso,
                "tenant": args.tenant,
                "candidates": [dataclasses.asdict(c) for c in truncated],
                "by_signal_count": {k: len(v) for k, v in by_signal.items()},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Wrote %d candidates (%s) to %s",
        len(truncated),
        ", ".join(f"{k}={len(v[: args.top])}" for k, v in by_signal.items()) or "none",
        out_path,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
