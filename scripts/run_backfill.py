"""ADR-038 Phase H.7 — orphan-case backfill ops runner.

Wraps ``agents.backfill.backfill_orphan_cases`` and
``merge_orphan_cases_by_correlation`` in a small CLI so ops can run
the migration as a scheduled job (or one-off) without writing
Python. The two passes are deliberately separate functions in
``agents/backfill.py`` (Pass 2 needs a maintenance window); this
runner mirrors that split with explicit flags.

Usage::

    # Pass 1 only (idempotent, runnable any time):
    python scripts/run_backfill.py --pass 1

    # Both passes (Pass 2 needs a quiet window):
    python scripts/run_backfill.py --pass both

    # Pass 2 dry-run — report-only, no merges applied:
    python scripts/run_backfill.py --pass 2 --dry-run

    # Provide a JSON customer-tier lookup for SLA stamping:
    python scripts/run_backfill.py --pass 1 --tier-map ops/tiers.json

Exit codes:
    0  success (report printed to stdout as JSON).
    2  invalid arguments / customer-tier lookup not found.
    3  unexpected runtime error (traceback to stderr).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from agents.backfill import (
    BackfillReport,
    backfill_orphan_cases,
    merge_orphan_cases_by_correlation,
)


def _load_tier_map(path: Optional[str]) -> Optional[Dict[str, str]]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"--tier-map path does not exist: {path}",
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"--tier-map JSON must be an object mapping customer_id "
            f"→ tier_name; got {type(raw).__name__} from {path}",
        )
    return {str(k): str(v) for k, v in raw.items()}


def _report_to_dict(report: BackfillReport, *, label: str) -> Dict[str, Any]:
    body = dataclasses.asdict(report)
    body["pass"] = label
    return body


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the ADR-038 Phase H.7 orphan-case backfill (Pass 1) "
            "and the optional correlation-merge sweep (Pass 2)."
        ),
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help=(
            "Tenant id to backfill. Backfill is tenant-scoped (the "
            "DB path is tenant-isolated via RLS); run once per tenant."
        ),
    )
    parser.add_argument(
        "--pass",
        dest="pass_",
        choices=("1", "2", "both"),
        default="1",
        help="Which pass(es) to run. Default: 1 (orphan-case-per-record).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "For Pass 2 only: report what would merge but do not "
            "mutate the case store. No effect on Pass 1 (it is "
            "already idempotent — re-running is a no-op once "
            "every record has parent_case_id set)."
        ),
    )
    parser.add_argument(
        "--tier-map",
        help=(
            "Path to a JSON file mapping customer_id → "
            "tier_name (for SLA-deadline stamping during Pass 1). "
            "Customers absent from the map fall back to the "
            "policy's default_sla_hours."
        ),
    )
    parser.add_argument(
        "--bundle-version-at-open",
        default="legacy-pre-h2",
        help=(
            "L0 bundle-version stamp written onto backfilled cases. "
            "Default ('legacy-pre-h2') is the audit-recognisable "
            "sentinel for retroactive materialisation."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Emit DEBUG logs from the backfill module.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("asoe.backfill")

    try:
        tier_map = _load_tier_map(args.tier_map)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    reports: list[Dict[str, Any]] = []
    try:
        if args.pass_ in ("1", "both"):
            log.info("Running Pass 1 — orphan-case-per-record backfill.")
            r1 = backfill_orphan_cases(
                args.tenant,
                customer_tier_lookup=tier_map,
                bundle_version_at_open=args.bundle_version_at_open,
            )
            reports.append(_report_to_dict(r1, label="1"))
        if args.pass_ in ("2", "both"):
            log.info(
                "Running Pass 2 — correlation-merge sweep "
                "(dry_run=%s).",
                args.dry_run,
            )
            r2 = merge_orphan_cases_by_correlation(
                args.tenant, dry_run=args.dry_run,
            )
            reports.append(_report_to_dict(r2, label="2"))
    except Exception:
        log.exception("backfill failed")
        return 3

    print(json.dumps({"reports": reports}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
