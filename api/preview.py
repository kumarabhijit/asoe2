"""Attachment-preview format detection (ADR-043 §2.1).

The viewer must pick a renderer from the *validated magic bytes*, never the
attacker-supplied `mime_type`, and must **default-deny**: anything not on the
allowlist (notably SVG and HTML — XSS vectors) is download-only, not rendered.
Magic-byte selection is necessary but not sufficient (polyglots), so it pairs
with the sandbox/CSP isolation on the asoe-ui side.
"""

from __future__ import annotations

from typing import Optional

_PreviewFormat = str  # "pdf" | "image" | "text"


def detect_preview_format(data: bytes, declared_mime: Optional[str] = None) -> Optional[_PreviewFormat]:
    """Return the renderer key for `data`, or None if it must not be previewed.

    `declared_mime` is accepted for signature parity with callers but is
    deliberately **ignored** — selection is by bytes only.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 4:
        return None
    head = bytes(data[:16])

    # PDF
    if head.startswith(b"%PDF"):
        return "pdf"

    # Raster images (allowlisted).
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if head.startswith(b"\xff\xd8\xff"):  # JPEG
        return "image"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image"

    # Plain text / CSV — conservative: valid UTF-8, no NUL, and not markup.
    # This denies SVG (`<svg…`) and HTML (`<!doctype`/`<html`) by construction.
    if b"\x00" in head:
        return None
    try:
        decoded = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if decoded.lstrip()[:1] == "<":
        return None
    return "text"
