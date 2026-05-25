"""CP-B RED gate (ADR-043 §2.1) — renderer selected by validated magic bytes.

Test-first (`xfail(strict=True)`; removed at CP-C). The viewer must pick a
renderer from the *bytes*, never the attacker-supplied `mime_type`, and must
default-deny — SVG and anything not on the allowlist is download-only, not
rendered. Magic-byte selection is necessary but not sufficient (polyglots), so
this pairs with the sandbox/CSP assertions on the asoe-ui side.
"""

from __future__ import annotations

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
_SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
_HTML = b"<!doctype html><script>alert(1)</script>"


def test_allowlisted_formats_detected_from_magic_bytes():
    from api.preview import detect_preview_format

    assert detect_preview_format(_PDF) == "pdf"
    assert detect_preview_format(_PNG) == "image"
    assert detect_preview_format(_JPEG) == "image"


def test_svg_is_denied():
    from api.preview import detect_preview_format

    assert detect_preview_format(_SVG) is None  # XSS vector — never rendered


def test_unknown_or_active_content_is_denied_by_default():
    from api.preview import detect_preview_format

    assert detect_preview_format(_HTML) is None
    assert detect_preview_format(b"\x00\x01\x02 random") is None


def test_declared_mime_type_is_ignored():
    from api.preview import detect_preview_format

    # PNG bytes with a lying caller-supplied mime must still resolve by bytes.
    assert detect_preview_format(_PNG, declared_mime="application/pdf") == "image"
