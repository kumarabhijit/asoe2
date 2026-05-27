"""Nightly reconciliation — SAP block codebook vs case_intent table.

Phase 6 of docs/plans/case-intent-supergroup-implementation-plan.md.
Authority: docs/specs/case-intent-supergroup-requirements.md §3.9 +
§8.8 + §9 (steward change-control).

Reads the active set of SAP block codes from a snapshot (CSV today;
direct SAP connector in a follow-up), compares against active rows of
``case_intent.sap_block_code``, and reports:

  - NEW   — codes present in SAP but not yet mapped to a case_intent.
            These open a steward ticket; the case-intake path will tag
            cases hitting these codes with ``SG_BLOCK_UNMAPPED``
            (requirements §8.8) until the steward maps them.
  - STALE — codes in case_intent that no longer exist in SAP. Steward
            should deprecate (set ``deprecated_at``).
  - OK    — codes that match.

The script is intentionally side-effect-free for v1 — it prints a
report. A later commit wires the GitHub-issue-opener / ops alerter
once the steward team chooses a routing target.

Usage:
    python -m scripts.reconcile_sap_block_codes \\
        --sap-snapshot path/to/sap_block_codes.csv \\
        --database-url $DATABASE_URL

CSV columns (snapshot format): ``sap_block_code,sap_block_field,description``.
``sap_block_field`` is the SAP table this code lives in (LIFSK, LIFSP,
FAKSK, FAKSP, ABGRU, CMGST, Z_CUSTOM). The script keys on
``(sap_block_code, sap_block_field)`` because a 2-char code is reused
across tables (TVLS '01' != TVFS '01').
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


ALLOWED_SAP_FIELDS: frozenset[str] = frozenset({
    "LIFSK", "LIFSP", "FAKSK", "FAKSP", "ABGRU", "CMGST", "Z_CUSTOM",
})
"""SAP block-table identifiers permitted by the taxonomy seed (and by
the V017 CHECK constraint on ``case_intent.sap_block_field``). The
snapshot loader rejects any other value so a typo (e.g. ``LIFSL``)
doesn't silently produce a meaningless drift report."""


@dataclass(frozen=True)
class SapBlockCode:
    sap_block_code: str
    sap_block_field: str
    description: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.sap_block_code, self.sap_block_field)


@dataclass(frozen=True)
class ReconciliationReport:
    new_in_sap: list[SapBlockCode]
    """Codes in the SAP snapshot with no mapping in case_intent."""
    stale_in_db: list[tuple[str, str, str]]
    """(intent_code, sap_block_code, sap_block_field) — present in
    case_intent but absent from the SAP snapshot."""
    malformed_in_db: list[tuple[str, str]]
    """(intent_code, sap_block_code) — case_intent rows with
    ``sap_block_code`` set but ``sap_block_field`` NULL. Without the
    field we cannot reconcile against SAP; the seed must be fixed by
    the Steward."""
    matched_count: int

    @property
    def has_drift(self) -> bool:
        return bool(self.new_in_sap or self.stale_in_db or self.malformed_in_db)


# ---------------------------------------------------------------------------
# Pure functions (testable without a DB or filesystem)
# ---------------------------------------------------------------------------

def reconcile(
    sap_codes: Iterable[SapBlockCode],
    db_codes: Iterable[tuple[str, Optional[str], Optional[str]]],
) -> ReconciliationReport:
    """Compute the diff. ``db_codes`` is the iterable of
    ``(intent_code, sap_block_code, sap_block_field)`` tuples from
    ``case_intent``. Rows where ``sap_block_field`` is NULL but
    ``sap_block_code`` is set are surfaced as ``malformed_in_db`` —
    they can't be reconciled and indicate a seed bug."""
    sap_set = {c.key: c for c in sap_codes}
    db_keyed: dict[tuple[str, str], tuple[str, str, str]] = {}
    malformed: list[tuple[str, str]] = []
    for intent_code, sbc, sbf in db_codes:
        if sbc is None:
            continue  # CUSTOMER intent or sentinel — out of scope.
        if sbf is None:
            malformed.append((intent_code, sbc))
            continue
        db_keyed[(sbc, sbf)] = (intent_code, sbc, sbf)

    new_in_sap = [
        sap_set[k] for k in sorted(sap_set.keys() - db_keyed.keys())
    ]
    stale_in_db = [
        db_keyed[k] for k in sorted(db_keyed.keys() - sap_set.keys())
    ]
    matched = len(sap_set.keys() & db_keyed.keys())
    return ReconciliationReport(
        new_in_sap=new_in_sap,
        stale_in_db=stale_in_db,
        malformed_in_db=sorted(malformed),
        matched_count=matched,
    )


def load_sap_snapshot(path: Path) -> list[SapBlockCode]:
    """Read the SAP block codebook from a CSV snapshot.

    Rejects rows where ``sap_block_field`` is outside the allowed enum
    so a typo (e.g. ``LIFSL`` for ``LIFSK``) doesn't silently produce
    a meaningless drift report (review finding #5)."""
    rows: list[SapBlockCode] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"sap_block_code", "sap_block_field"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"SAP snapshot {path} missing required columns: {missing}"
            )
        for lineno, row in enumerate(reader, start=2):  # header is line 1
            sbc = (row["sap_block_code"] or "").strip()
            sbf = (row["sap_block_field"] or "").strip()
            if not sbc:
                raise ValueError(
                    f"SAP snapshot {path}:{lineno}: empty sap_block_code"
                )
            if sbf not in ALLOWED_SAP_FIELDS:
                raise ValueError(
                    f"SAP snapshot {path}:{lineno}: sap_block_field "
                    f"{sbf!r} not in {sorted(ALLOWED_SAP_FIELDS)}"
                )
            rows.append(SapBlockCode(
                sap_block_code=sbc,
                sap_block_field=sbf,
                description=(row.get("description") or "").strip(),
            ))
    return rows


def load_db_block_codes(
    conn: sqlite3.Connection,
) -> list[tuple[str, Optional[str], Optional[str]]]:
    """Read active (intent_code, sap_block_code, sap_block_field)
    triples from ``case_intent``.

    Rows with NULL ``sap_block_code`` (CUSTOMER intents, sentinels) are
    excluded — they're not SAP-mapped. Rows with ``sap_block_code`` set
    but ``sap_block_field`` NULL ARE returned: the reconciler surfaces
    them as ``malformed_in_db`` so the Steward sees the seed bug
    explicitly (review finding #4)."""
    cur = conn.execute(
        """
        SELECT code, sap_block_code, sap_block_field
        FROM case_intent
        WHERE is_active = 1
          AND sap_block_code IS NOT NULL
        ORDER BY sap_block_code, COALESCE(sap_block_field, '')
        """
    )
    return [(intent, sbc, sbf) for intent, sbc, sbf in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(report: ReconciliationReport) -> str:
    lines: list[str] = []
    lines.append(f"=== SAP block-code reconciliation ===")
    lines.append(f"matched: {report.matched_count}")
    lines.append(f"new in SAP (steward action required): {len(report.new_in_sap)}")
    for code in report.new_in_sap:
        lines.append(
            f"  + {code.sap_block_code:6}  {code.sap_block_field:8}  {code.description}"
        )
    lines.append(f"stale in DB (consider deprecation): {len(report.stale_in_db)}")
    for intent_code, sbc, sbf in report.stale_in_db:
        lines.append(f"  - {sbc:6}  {sbf:8}  -> {intent_code}")
    lines.append(
        f"malformed in DB (sap_block_code set, sap_block_field NULL): "
        f"{len(report.malformed_in_db)}"
    )
    for intent_code, sbc in report.malformed_in_db:
        lines.append(f"  ! {sbc:6}  (no field)  -> {intent_code}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sap-snapshot", type=Path, required=True,
        help="Path to the SAP block-code CSV snapshot.",
    )
    parser.add_argument(
        "--database-url", type=str, required=False,
        help="SQLite DATABASE_URL. Postgres support is a follow-up.",
    )
    parser.add_argument(
        "--exit-nonzero-on-drift", action="store_true",
        help=(
            "Return exit code 2 when drift is detected. Useful in a "
            "scheduled CI job that opens a steward issue on non-zero."
        ),
    )
    args = parser.parse_args(argv)

    sap_codes = load_sap_snapshot(args.sap_snapshot)
    if not args.database_url or not args.database_url.startswith("sqlite"):
        print("--database-url is required and must be sqlite:/// for now.")
        return 1
    db_path = args.database_url.replace("sqlite:///", "").replace("sqlite://", "")
    conn = sqlite3.connect(db_path)
    try:
        db_codes = load_db_block_codes(conn)
    finally:
        conn.close()

    report = reconcile(sap_codes, db_codes)
    print(format_report(report))
    if args.exit_nonzero_on_drift and report.has_drift:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
