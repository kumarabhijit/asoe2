"""PARITY-5 — PII redaction at the structured-logger level.

Security review requirement: the Application Insights exporter must
never see plaintext PII. We apply best-effort regex redaction at the
log-formatter level so a log call like::

    log.info("operator %s triggered %s on case %s",
             user.email, recipe, case_id)

…renders with ``<redacted-email>`` in the email position before the
record reaches the exporter.

Patterns covered today: email addresses, North-American phone numbers,
US SSNs. The list is intentionally conservative — false negatives are
preferred to false positives that mask debugging signal. Domain-
specific redactions (credit card, account numbers) live in the
adapter callers that emit those fields.
"""

from __future__ import annotations

import re
from typing import Pattern

_EMAIL_RE: Pattern[str] = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_PHONE_RE: Pattern[str] = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
_SSN_RE: Pattern[str] = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def redact_pii(value: str) -> str:
    """Return ``value`` with the standard PII patterns replaced by
    ``<redacted-...>`` placeholders. Idempotent (a string that contains
    no PII is returned unchanged)."""
    if not value:
        return value
    out = _EMAIL_RE.sub("<redacted-email>", value)
    out = _PHONE_RE.sub("<redacted-phone>", out)
    out = _SSN_RE.sub("<redacted-ssn>", out)
    return out
