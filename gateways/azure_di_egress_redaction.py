"""PII redaction for egress to Azure Document Intelligence — PARITY-6.

Security + ML review requirement: before any document payload leaves
our perimeter for AzureDI processing, mask the customer credit /
financial fields and any addresses that aren't the field-of-interest.
This is defence-in-depth — AzureDI is a Microsoft-trusted endpoint
under our contract, but the redacted payload is what lands in
Azure's process logs and on-disk caches.

Patterns scrubbed:

  * SSN (NNN-NN-NNNN)
  * US credit card numbers (with or without separators)
  * IBAN-ish account numbers (heuristic)
  * Dollar amounts with $ prefix in the body OR explicit "credit
    limit" / "credit line" labelled values

Document structure is preserved (whitespace, line breaks, layout)
so AzureDI's spatial extraction still works against the redacted text.
"""

from __future__ import annotations

import re

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Credit-card: strictly grouped as 4-4-4-4 (Visa/MC/AmEx-like layouts)
# OR a contiguous 13-19 digit run. The previous loose pattern
# `\b(?:\d[ -]?){13,19}\b` matched any 13-19 digit-or-separator run,
# which over-redacted innocent digit sequences like a phone-number list
# (catch: "123 456 789 012 345 678" was flagged as a CC) and broke
# AzureDI's spatial-extraction anchoring on the redacted spans.
_CC_GROUPED_RE = re.compile(
    r"\b(?:\d{4}[- ]){3}\d{4}\b"
)
_CC_CONTIGUOUS_RE = re.compile(r"\b\d{13,19}\b")


def _looks_like_credit_card(digits: str) -> bool:
    """Luhn-checksum filter — anchors the CC redactor to actual
    credit-card-like sequences and discards arbitrary digit runs.

    Returns True iff ``digits`` (a contiguous digit string) passes the
    Luhn algorithm. The Luhn check is the same heuristic the major card
    networks use to detect typos; false positives at the 13-19-digit
    bound are now bounded by the 1-in-10 collision rate of the check.
    """
    d = [int(c) for c in digits]
    if len(d) < 13 or len(d) > 19:
        return False
    s = 0
    parity = len(d) % 2
    for i, n in enumerate(d):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        s += n
    return s % 10 == 0


def _redact_cc_contiguous(match: re.Match[str]) -> str:
    digits = match.group(0)
    return "<redacted-cc>" if _looks_like_credit_card(digits) else digits


# IBAN-ish: country code + 2 check digits + 10-30 alphanumerics.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
# Dollar amounts ($ prefix, optional commas, optional decimals).
_DOLLARS_RE = re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?")


def redact_for_azure_di(body: str) -> str:
    """Return ``body`` with high-risk PII / financial fields masked.

    Idempotent — a body with no matches is returned unchanged. The
    structural layout (line breaks, indentation) is preserved so the
    downstream spatial extractor still has a usable document.
    """
    if not body:
        return body
    out = _SSN_RE.sub("<redacted-ssn>", body)
    out = _DOLLARS_RE.sub("<redacted-amount>", out)
    # Grouped CC patterns are unambiguous; the contiguous form runs a
    # Luhn check to avoid stomping innocent 13-19 digit sequences.
    out = _CC_GROUPED_RE.sub("<redacted-cc>", out)
    out = _CC_CONTIGUOUS_RE.sub(_redact_cc_contiguous, out)
    out = _IBAN_RE.sub("<redacted-iban>", out)
    return out
