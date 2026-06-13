"""Convert raw `.eml` intake fixtures into `email_intake.fetch_message` payloads.

RFC: asoe-ui/docs/synthetic-data-placement-rfc.md (Decision A, Phase 4).

The 9 synthetic intake emails were authored in asoe-ui under
`data/synthetic/emails/`. They belong below the gateway port, in asoe2,
because they feed the email-intake path (the UI only renders results).

`gateways/msgraph_intake.py` does NOT ingest raw RFC-822 — its live
backend reads Microsoft Graph JSON and its `fetch_message` operation
returns a domain-shaped payload:

    {source_email_id, from_address, received_at,            # audit-bearing
     subject, body_excerpt, body_hash, attachment_manifest} # derived

So the `.eml` files are *converted* (not copied) into that shape. The
raw `.eml` stays committed as the human-readable source of truth; the
converted JSON is what an `email_intake` recorded/stub backend serves.

This module is import-safe (no side effects) so the parse/intake test
(`tests/test_email_intake_fixtures.py`) can re-derive the payloads and
diff them against the committed JSON as a drift gate. Run as a script to
(re)materialise the committed fixtures:

    python scripts/convert_eml_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "email_intake"
RAW_DIR = FIXTURE_DIR / "raw"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

_BODY_EXCERPT_CHARS = 280


# RFC 6532 policy: the synthetic fixtures carry literal UTF-8 (8-bit) header
# bytes (em-dashes in Subject lines) rather than RFC-2047 encoded words. The
# modern policy decodes both correctly; the legacy parser mojibakes the 8-bit
# case.
_POLICY = policy.default.clone(utf8=True)


def _plain_body(msg: EmailMessage) -> str:
    """Return the decoded text/plain body of an RFC-822 message."""
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        return body.get_content()
    if not msg.is_multipart():
        return msg.get_content()
    return ""


def _attachment_manifest(msg: EmailMessage) -> List[Dict[str, Any]]:
    manifest: List[Dict[str, Any]] = []
    for part in msg.iter_attachments():
        name = part.get_filename()
        if not name:
            continue
        payload = part.get_content()
        nbytes = len(payload if isinstance(payload, (bytes, bytearray))
                     else str(payload).encode("utf-8"))
        manifest.append({
            "name": name,
            "mime_type": part.get_content_type(),
            "bytes": nbytes,
        })
    return manifest


def eml_to_fetch_message(
    raw: bytes,
    *,
    source_email_id: str,
    received_at: str,
) -> Dict[str, Any]:
    """Project a raw `.eml` byte stream onto the fetch_message payload.

    `source_email_id` and `received_at` are passed in (sourced from the
    manifest) rather than derived from headers so the audit-bearing keys
    are stable even when a fixture lacks a Message-ID / has a header date
    that differs from the canonical intake timestamp.
    """
    msg = message_from_bytes(raw, policy=_POLICY)
    body = _plain_body(msg)
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    excerpt = " ".join(body.split())[:_BODY_EXCERPT_CHARS]
    return {
        "source_email_id": source_email_id,
        "from_address": str(msg.get("From", "")),
        "received_at": received_at,
        "subject": str(msg.get("Subject", "")),
        "body_excerpt": excerpt,
        "body_hash": body_hash,
        "attachment_manifest": _attachment_manifest(msg),
    }


def _source_email_id(entry: Dict[str, Any]) -> str:
    return entry.get("zemail_msg_id") or Path(entry["file"]).stem


def build_recorded(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Build the full recorded fixture dict for one manifest entry."""
    raw = (RAW_DIR / entry["file"]).read_bytes()
    output = eml_to_fetch_message(
        raw,
        source_email_id=_source_email_id(entry),
        received_at=entry["date"],
    )
    return {
        "case": Path(entry["file"]).stem,
        "gateway": "email_intake",
        "operation": "fetch_message",
        "source_eml": f"raw/{entry['file']}",
        "scenario": entry.get("scenario"),
        "expected_classification": entry.get("expected_classification"),
        "ref_po": entry.get("ref_po"),
        "note": entry.get("note"),
        "recorded_by": "scripts/convert_eml_fixtures.py",
        "output": output,
    }


def load_manifest() -> List[Dict[str, Any]]:
    return json.loads(MANIFEST_PATH.read_text())


def recorded_path(entry: Dict[str, Any]) -> Path:
    return FIXTURE_DIR / f"{Path(entry['file']).stem}.fetch_message.json"


def main() -> None:
    manifest = load_manifest()
    for entry in manifest:
        rec = build_recorded(entry)
        path = recorded_path(entry)
        path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
