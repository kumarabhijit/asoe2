"""Steward CLI — propose changes to the case taxonomy.

Phase 6 of docs/plans/case-intent-supergroup-implementation-plan.md.
Authority: docs/specs/case-intent-supergroup-requirements.md §9 (steward
change-control), §3.9 (SAP block-code reconciliation).

The steward role owns the taxonomy YAML at ``db/seeds/case_taxonomy.yaml``.
This CLI is the thin layer that:

  * generates a fresh row (intent or supergroup) and writes it into
    the YAML in the right place,
  * re-runs the constants generator so the committed Python + TS
    constants stay byte-stable,
  * validates the result against the JSON-schema + structural
    invariants so a bad proposal fails at the steward's terminal,
    not in CI.

The CLI does NOT auto-merge to main. The expected flow is::

    steward_change add-intent --code INT_PALLET_DAMAGE \\
        --supergroup SG_BLOCK_LOGISTICS \\
        --description "Damaged-pallet receipt rejection" \\
        --sap-block-code ZD --sap-block-field LIFSP
    git diff db/seeds/case_taxonomy.yaml \\
        contracts/_generated/taxonomy_constants.py \\
        asoe-ui/src/generated/taxonomy.ts
    git commit -m "taxonomy: add INT_PALLET_DAMAGE"
    gh pr create ...

Usage:
    steward_change add-intent --code INT_X --supergroup SG_Y --description "..."
    steward_change add-supergroup --code SG_X --origin CUSTOMER|API --description "..."
    steward_change deprecate-intent --code INT_X [--replaced-by INT_Y]
    steward_change validate
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from scripts.seed_taxonomy import REPO_ROOT, SEED_PATH, load_yaml
from scripts.generate_taxonomy_constants import generate as _generate_constants
from scripts.generate_taxonomy_constants import PY_OUT, TS_OUT


# ---------------------------------------------------------------------------
# YAML I/O — preserve order, key set, formatting (best-effort)
# ---------------------------------------------------------------------------

def _load_raw() -> dict[str, Any]:
    """Read the YAML *without* structural validation. ``load_yaml`` from
    seed_taxonomy validates + raises; for editing we want to read the
    current state even if it's mid-edit, then validate at write time."""
    return yaml.safe_load(SEED_PATH.read_text(encoding="utf-8"))


def _dump_raw(data: dict[str, Any]) -> None:
    """Write the YAML back. ``sort_keys=False`` preserves the
    Steward-curated ordering; ``default_flow_style=False`` keeps the
    block style the seed uses."""
    SEED_PATH.write_text(
        yaml.safe_dump(
            data, sort_keys=False, default_flow_style=False, allow_unicode=True,
            width=120,
        ),
        encoding="utf-8",
    )


def _validate_and_regenerate() -> None:
    """Validate the new state and regenerate constants. If either step
    fails the YAML is left as-is — the steward's terminal sees the
    error and reverts manually."""
    load_yaml()  # raises ValueError on invariant break
    py_text, ts_text = _generate_constants()
    PY_OUT.write_bytes(py_text.encode("utf-8"))
    TS_OUT.write_bytes(ts_text.encode("utf-8"))


def _safe_relative(p: Path) -> str:
    """``Path.relative_to`` raises when the target is outside the
    base. Tests redirect the seed/constants paths to ``tmp_path``;
    use the absolute path as a display fallback so the CLI doesn't
    crash on test runs."""
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# add-intent
# ---------------------------------------------------------------------------

def add_intent(args: argparse.Namespace) -> int:
    data = _load_raw()
    intent_codes = {i["code"] for i in data["intents"]}
    if args.code in intent_codes:
        print(f"error: intent {args.code!r} already exists in seed.", file=sys.stderr)
        return 2
    supergroup_codes = {s["code"] for s in data["supergroups"]}
    if args.supergroup not in supergroup_codes:
        print(
            f"error: supergroup {args.supergroup!r} not in seed. "
            f"Known: {sorted(supergroup_codes)}",
            file=sys.stderr,
        )
        return 2

    new_intent: dict[str, Any] = {
        "code": args.code,
        "supergroup_code": args.supergroup,
        "description": args.description,
        "sap_block_code": args.sap_block_code,
        "sap_block_field": args.sap_block_field,
    }
    if args.phase_zero_pending:
        new_intent["phase_zero_pending"] = True
    data["intents"].append(new_intent)

    # Default display label for the steward's locale (en). Steward can
    # edit later to refine the human label.
    label = {
        "code": args.code, "domain": "INTENT",
        "display_name": args.display_name or _humanise(args.code),
    }
    data.setdefault("labels", []).append(label)

    _dump_raw(data)
    try:
        _validate_and_regenerate()
    except Exception as exc:
        print(f"error: proposal failed validation — {exc}", file=sys.stderr)
        print(
            f"YAML at {SEED_PATH} now reflects the proposed state; "
            "revert with `git checkout db/seeds/case_taxonomy.yaml` if "
            "you want to abandon.",
            file=sys.stderr,
        )
        return 3

    print(f"added intent {args.code} under {args.supergroup}.")
    print("modified:")
    for p in (SEED_PATH, PY_OUT, TS_OUT):
        print(f"  {_safe_relative(p)}")
    print("Next: review the diff, commit, open PR.")
    return 0


# ---------------------------------------------------------------------------
# add-supergroup
# ---------------------------------------------------------------------------

def add_supergroup(args: argparse.Namespace) -> int:
    data = _load_raw()
    sg_codes = {s["code"] for s in data["supergroups"]}
    if args.code in sg_codes:
        print(f"error: supergroup {args.code!r} already exists.", file=sys.stderr)
        return 2

    # Pick a sort_order: put the new one at the end of its origin's
    # block + 10 so manual interleaving stays easy.
    same_origin_orders = [
        s["sort_order"] for s in data["supergroups"]
        if s["origin"] == args.origin
    ]
    next_order = (max(same_origin_orders) + 10) if same_origin_orders else 100

    new_sg = {
        "code": args.code,
        "origin": args.origin,
        "description": args.description,
        "owner_role": args.owner_role,
        "sort_order": next_order,
    }
    data["supergroups"].append(new_sg)
    data.setdefault("labels", []).append({
        "code": args.code, "domain": "SUPERGROUP",
        "display_name": args.display_name or _humanise(args.code),
    })

    _dump_raw(data)
    try:
        _validate_and_regenerate()
    except Exception as exc:
        print(f"error: proposal failed validation — {exc}", file=sys.stderr)
        return 3

    print(f"added supergroup {args.code} ({args.origin}, sort_order={next_order}).")
    return 0


# ---------------------------------------------------------------------------
# deprecate-intent
# ---------------------------------------------------------------------------

def deprecate_intent(args: argparse.Namespace) -> int:
    data = _load_raw()
    target = next((i for i in data["intents"] if i["code"] == args.code), None)
    if target is None:
        print(f"error: intent {args.code!r} not in seed.", file=sys.stderr)
        return 2
    if target.get("deprecated_at"):
        print(
            f"error: intent {args.code!r} already deprecated at "
            f"{target['deprecated_at']}.",
            file=sys.stderr,
        )
        return 2
    if args.replaced_by:
        replacement = next(
            (i for i in data["intents"] if i["code"] == args.replaced_by), None,
        )
        if replacement is None:
            print(
                f"error: replacement intent {args.replaced_by!r} not in seed.",
                file=sys.stderr,
            )
            return 2

    target["deprecated_at"] = (args.effective or date.today()).isoformat()
    if args.replaced_by:
        target["replaced_by_code"] = args.replaced_by

    _dump_raw(data)
    try:
        _validate_and_regenerate()
    except Exception as exc:
        print(f"error: proposal failed validation — {exc}", file=sys.stderr)
        return 3

    print(f"deprecated intent {args.code} effective {target['deprecated_at']}.")
    if args.replaced_by:
        print(f"  replaced_by: {args.replaced_by}")
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def validate(args: argparse.Namespace) -> int:
    """Run JSON-schema + structural invariants on the current YAML and
    confirm the generated constants are byte-stable against it.
    Returns 0 if clean, 4 if drift / 5 if invalid."""
    del args
    try:
        load_yaml()
    except Exception as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 5
    py_text, ts_text = _generate_constants()
    py_disk = PY_OUT.read_bytes() if PY_OUT.exists() else b""
    ts_disk = TS_OUT.read_bytes() if TS_OUT.exists() else b""
    drift = []
    if py_disk != py_text.encode("utf-8"):
        drift.append(_safe_relative(PY_OUT))
    if ts_disk != ts_text.encode("utf-8"):
        drift.append(_safe_relative(TS_OUT))
    if drift:
        print(
            "validation: YAML parsed but generated constants are out of sync: "
            f"{drift}. Run "
            "`python -m scripts.generate_taxonomy_constants` and commit.",
            file=sys.stderr,
        )
        return 4
    print("validation: OK")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _humanise(code: str) -> str:
    """``SG_BLOCK_PRICING`` → ``Pricing Block`` (best-effort default
    label; steward should edit if they want a different label)."""
    stripped = code.split("_", 1)[1] if "_" in code else code
    words = stripped.replace("_", " ").title().split()
    return " ".join(words)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add_int = sub.add_parser("add-intent", help="Add a new leaf intent.")
    p_add_int.add_argument("--code", required=True,
                            help="Intent code, e.g. INT_PALLET_DAMAGE.")
    p_add_int.add_argument("--supergroup", required=True,
                            help="Parent supergroup code.")
    p_add_int.add_argument("--description", required=True,
                            help="Business description (min 10 chars).")
    p_add_int.add_argument("--sap-block-code", default=None,
                            help="SAP block-reason code (optional).")
    p_add_int.add_argument(
        "--sap-block-field", default=None,
        choices=[None, "LIFSK", "LIFSP", "FAKSK", "FAKSP", "ABGRU", "CMGST", "Z_CUSTOM"],
        help="SAP block-table identifier (required if sap-block-code is set).",
    )
    p_add_int.add_argument("--phase-zero-pending", action="store_true",
                            help="Mark as provisional pending Phase 0 confirmation.")
    p_add_int.add_argument("--display-name", default=None,
                            help="Human-readable English label.")
    p_add_int.set_defaults(func=add_intent)

    p_add_sg = sub.add_parser("add-supergroup", help="Add a new supergroup.")
    p_add_sg.add_argument("--code", required=True,
                          help="Supergroup code, e.g. SG_BLOCK_TAX.")
    p_add_sg.add_argument("--origin", required=True,
                          choices=["CUSTOMER", "API", "BOTH"])
    p_add_sg.add_argument("--description", required=True,
                          help="Business description.")
    p_add_sg.add_argument("--owner-role", required=True,
                          help="Role name (snake_case).")
    p_add_sg.add_argument("--display-name", default=None,
                          help="Human-readable English label.")
    p_add_sg.set_defaults(func=add_supergroup)

    p_dep = sub.add_parser("deprecate-intent",
                            help="Deprecate an existing intent.")
    p_dep.add_argument("--code", required=True)
    p_dep.add_argument("--replaced-by", default=None,
                       help="Replacement intent code (optional).")
    p_dep.add_argument(
        "--effective", default=None, type=date.fromisoformat,
        help="Effective date YYYY-MM-DD (defaults to today).",
    )
    p_dep.set_defaults(func=deprecate_intent)

    p_val = sub.add_parser("validate",
                            help="Validate the YAML + check constants drift.")
    p_val.set_defaults(func=validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
