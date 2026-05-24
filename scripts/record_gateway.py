"""Record a frozen gateway output for the deterministic replay seam.

Strategy §3 (recorded-fixture boundary): red-green TDD never hits a live model.
The order-extraction gateway is exercised in CI through
`gateways.recorded_backend.RecordedGatewayBackend`, which replays the JSON
fixtures this script mints. Recordings are produced by a deliberate, reviewed
run against the LIVE constrained-generation backend and are **never
auto-refreshed in CI**.

Usage:
    # Requires the heavy `outlines`/`torch` extra to be installed.
    python scripts/record_gateway.py \
        --case walmart_pdf \
        --source-type PDF \
        --email path/to/email.txt \
        [--attachment path/to/attachment.txt]

Output:
    tests/fixtures/gateway/order_extraction/<case>.recorded.json
        { case, gateway, operation, model_id, prompt_hash, recorded_by, output }

The committed fixture is the contract. Review it (and the prompt_hash provenance)
the same way you review code; do not regenerate it casually.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from gateways.extraction import GATEWAY_NAME, OP_EXTRACT_ORDER
from llm.sanitizer import sanitize_email_text_for_llm

_FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "gateway"


def _read(path: str | None) -> str:
    return Path(path).read_text() if path else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, help="Stable case id (the replay key).")
    parser.add_argument("--source-type", default="EMAIL_BODY")
    parser.add_argument("--email", required=True, help="Path to the email body text.")
    parser.add_argument("--attachment", default=None, help="Optional attachment text.")
    parser.add_argument("--model-name", default=None)
    args = parser.parse_args()

    # Lazy import — only this manual path needs the live model.
    from gateways.outlines_extraction import OutlinesExtractionBackend

    safe_text = sanitize_email_text_for_llm(
        _read(args.email), attachment_text=_read(args.attachment)
    )
    backend = OutlinesExtractionBackend(args.model_name)
    envelope = backend.extract_order(
        safe_text=safe_text, source_type=args.source_type, hint={"case": args.case}
    )

    prompt = OutlinesExtractionBackend.extract_order_prompt(
        safe_text=safe_text, source_type=args.source_type
    )
    prompt_hash = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    record = {
        "case": args.case,
        "gateway": GATEWAY_NAME,
        "operation": OP_EXTRACT_ORDER,
        "model_id": f"outlines:{args.model_name or 'default'}",
        "prompt_hash": prompt_hash,
        "recorded_by": "scripts/record_gateway.py",
        "output": envelope.model_dump(),
    }

    out_dir = _FIXTURE_ROOT / GATEWAY_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.case}.recorded.json"
    out_path.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
