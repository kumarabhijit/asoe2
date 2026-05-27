"""Cost-pricing simulation for AzureDI extraction — PARITY-7.

Per ML review: ``EXTRACTION_MAX_COST_USD_PER_PAGE = 0.05`` may be too
low for custom-extract (typical p95 $0.08–$0.15/page). Run this
script BEFORE the provider decision locks (Q2 open question follow-
up): if the simulation says custom-extract is needed, raise the
ceiling to a realistic value (≥ $0.10–$0.15) in the SAME PR that
switches the provider.

Inputs: a number of POs and per-PO page distribution. Outputs:
expected USD cost under each candidate provider configuration, and a
verdict against the configured ceiling.

This script does NOT call AzureDI. It runs against the pricing
constants below; refresh those when AzureDI publishes a price
change.

Usage:
    python scripts/simulate_extraction_cost.py --n 50

The script prints a Markdown table operators paste into the PR
description for the provider-lock decision.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    usd_per_page: float
    notes: str


# Pricing snapshot 2026-05-26. Confirm against the AzureDI pricing
# page before any provider-lock PR; document any delta in the same PR.
PROFILES: List[ProviderProfile] = [
    ProviderProfile(
        name="prebuilt-invoice",
        usd_per_page=0.010,
        notes="Decision Q2 baseline. Within current 0.05 ceiling.",
    ),
    ProviderProfile(
        name="prebuilt-document",
        usd_per_page=0.015,
        notes="Generic; less invoice-specific. Within ceiling.",
    ),
    ProviderProfile(
        name="custom-extract",
        usd_per_page=0.080,  # p50; p95 ≈ 0.15.
        notes="Custom-trained per-tenant. p50 0.08, p95 0.15.",
    ),
    ProviderProfile(
        name="custom-extract-p95",
        usd_per_page=0.150,
        notes="Custom-trained p95; raise EXTRACTION_MAX_COST_USD_PER_PAGE.",
    ),
]


# Typical per-PO page count distribution from observed inbox traffic
# (synthetic; replace with a real sample when available).
PAGE_COUNTS_DEFAULT = [1, 1, 1, 2, 2, 2, 3, 3, 4, 6]


def simulate(
    *,
    n_pos: int,
    page_counts: List[int],
    ceiling_usd_per_page: float,
) -> str:
    """Return a markdown report of expected cost under each profile."""
    total_pages = sum(
        page_counts[i % len(page_counts)] for i in range(n_pos)
    )
    median_pages = statistics.median(page_counts)
    p95_pages = sorted(page_counts)[max(0, int(0.95 * (len(page_counts) - 1)))]

    lines: List[str] = []
    lines.append(f"# Extraction cost simulation (n={n_pos} POs)\n")
    lines.append(f"Total pages: {total_pages}")
    lines.append(f"Median pages/PO: {median_pages}")
    lines.append(f"p95 pages/PO: {p95_pages}")
    lines.append(f"Configured ceiling: ${ceiling_usd_per_page:.3f}/page\n")
    lines.append("| Provider | $/page | total $ | within ceiling | notes |")
    lines.append("|---|---|---|---|---|")
    for p in PROFILES:
        total = p.usd_per_page * total_pages
        within = "yes" if p.usd_per_page <= ceiling_usd_per_page else "NO — raise ceiling"
        lines.append(
            f"| {p.name} | ${p.usd_per_page:.3f} | ${total:.2f} | "
            f"{within} | {p.notes} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50, help="Number of POs to simulate")
    parser.add_argument(
        "--ceiling", type=float, default=0.05,
        help="Configured EXTRACTION_MAX_COST_USD_PER_PAGE",
    )
    args = parser.parse_args(argv)

    report = simulate(
        n_pos=args.n,
        page_counts=PAGE_COUNTS_DEFAULT,
        ceiling_usd_per_page=args.ceiling,
    )
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
