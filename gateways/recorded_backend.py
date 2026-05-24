from __future__ import annotations

# RecordedGatewayBackend — the replay seam for non-deterministic gateway reads.
#
# Strategy §3 (recorded-fixture boundary): red-green never hits a live model.
# The fixture boundary is the *gateway backend interface*, not the model. This
# backend is a sibling-in-spirit of `constraints.DeterministicFallbackBackend`:
# it replays a frozen constrained-generation output so the extraction gateway,
# recipes, and the composer can be TDD'd deterministically — "given this
# extraction, the projection must produce exactly X."
#
# Recordings live under `tests/fixtures/gateway/<gateway>/<case>.recorded.json`,
# each carrying the constrained-generation `output` plus its `model_id` and
# `prompt_hash` provenance. They are produced by a deliberate, reviewed
# `scripts/record_gateway.py` run and are NEVER auto-refreshed in CI.

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

from gateways.extraction import GATEWAY_NAME, ExtractedOrderEnvelope

# Golden recordings are test data — they live under tests/fixtures/.
_FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "gateway"

# Hint keys, in priority order, used to resolve which recording to replay. In
# production the live backend reads `safe_text`; the replay double keys on the
# stable case / order identifier the request already carries.
_CASE_HINT_KEYS = ("case", "recording_case", "order_id")


class RecordedGatewayBackend:
    """Replays frozen extraction-gateway output for deterministic TDD."""

    def __init__(
        self,
        recordings_dir: Path | str | None = None,
        *,
        gateway: str = GATEWAY_NAME,
    ) -> None:
        base = (
            Path(recordings_dir)
            if recordings_dir is not None
            else _FIXTURE_ROOT / gateway
        )
        self._dir = base
        self._by_case: Dict[str, dict] = {}
        if base.is_dir():
            for path in sorted(base.glob("*.recorded.json")):
                rec = json.loads(path.read_text())
                case = rec.get("case")
                if case:
                    self._by_case[str(case)] = rec

    @property
    def cases(self) -> List[str]:
        return sorted(self._by_case)

    def _resolve_case(self, hint: Mapping[str, Any]) -> str:
        for key in _CASE_HINT_KEYS:
            value = hint.get(key)
            if value:
                return str(value)
        raise KeyError(
            "RecordedGatewayBackend: request carries no case/order_id hint to "
            f"resolve a recording (looked for {_CASE_HINT_KEYS})"
        )

    def extract_order(
        self, *, safe_text: str, source_type: str, hint: Mapping[str, Any]
    ) -> ExtractedOrderEnvelope:
        case = self._resolve_case(hint)
        rec = self._by_case.get(case)
        if rec is None:
            # Fail loud — a missing recording must never silently fabricate an
            # order (Guardrail #5/#6). Add a fixture via scripts/record_gateway.py.
            raise KeyError(
                f"No recorded gateway output for case {case!r} under {self._dir} "
                f"(have: {self.cases})"
            )
        return ExtractedOrderEnvelope.model_validate(rec["output"])
