"""ADR-042 §5 Phase-0 HARD GATE — autonomy vocabulary versioning.

Product decision: adopt the prototype's L1-L4 ordering (L1 = most autonomous
... L4 = human escalation) as v2, WITHOUT mutating the canonical meaning of
historical records. The backend's current (v1) ordering is the inverse:
L1 = observe only (least autonomous) ... L4 = full autonomy (ADR-034,
contracts/policy.py:46,226).

This is written TEST-FIRST and is RED until a versioned autonomy resolver
exists. Per the autonomous operating contract, the *production* flip of the
canonical vocabulary requires human + compliance dual-control sign-off; this
test is the safety net that flip must satisfy.

Implementation target (to be created under the gate): `contracts.autonomy`
exposing:
  * CURRENT_AUTONOMY_VOCAB_VERSION: str
  * autonomy_rank(level: str, vocab_version: str) -> int
      degree of automation; higher == more autonomous.
"""

from __future__ import annotations

import pytest

try:
    from contracts.autonomy import (  # type: ignore
        CURRENT_AUTONOMY_VOCAB_VERSION,
        autonomy_rank,
    )

    _IMPLEMENTED = True
except ImportError:
    _IMPLEMENTED = False

_GATE = "ADR-042 §5: contracts.autonomy versioned resolver not implemented yet"

# Implemented 2026-05-24 — the autonomy v2 (prototype-ordering) vocabulary
# landed once the compliance sign-off gate was waived for pre-production
# (owner veto). The xfail marker is removed; these now assert for real.


def test_product_decision_current_vocab_is_v2() -> None:
    assert _IMPLEMENTED, _GATE
    # Product directive (2026-05-23): prototype ordering is the live vocabulary.
    assert CURRENT_AUTONOMY_VOCAB_VERSION == "v2"


def test_v1_ordering_is_observe_low_to_full_auto_high() -> None:
    """v1 (legacy/current policy.py): L1 = least autonomous, L4 = most."""
    assert _IMPLEMENTED, _GATE
    assert autonomy_rank("L1", "v1") < autonomy_rank("L4", "v1")
    assert (
        autonomy_rank("L1", "v1")
        < autonomy_rank("L2", "v1")
        < autonomy_rank("L3", "v1")
        < autonomy_rank("L4", "v1")
    )


def test_v2_ordering_is_auto_high_to_human_low() -> None:
    """v2 (prototype/product): L1 = most autonomous, L4 = human (least)."""
    assert _IMPLEMENTED, _GATE
    assert autonomy_rank("L1", "v2") > autonomy_rank("L4", "v2")
    assert (
        autonomy_rank("L1", "v2")
        > autonomy_rank("L2", "v2")
        > autonomy_rank("L3", "v2")
        > autonomy_rank("L4", "v2")
    )


def test_historical_records_are_not_silently_reinterpreted() -> None:
    """The same level string MUST resolve to OPPOSITE meaning across versions.

    A record stamped v1 keeps its v1 meaning forever even though v2 is current.
    If this ever returns equal ranks, a historical 'L1' attestation has been
    silently flipped — the audit-integrity catastrophe ADR-042 §5 guards.
    """
    assert _IMPLEMENTED, _GATE
    assert autonomy_rank("L1", "v1") != autonomy_rank("L1", "v2")
    assert autonomy_rank("L4", "v1") != autonomy_rank("L4", "v2")
    # The most-autonomous tier under each version maps to opposite labels.
    assert autonomy_rank("L4", "v1") == autonomy_rank("L1", "v2")


def test_unknown_version_fails_closed() -> None:
    assert _IMPLEMENTED, _GATE
    with pytest.raises((KeyError, ValueError)):
        autonomy_rank("L1", "v999")
