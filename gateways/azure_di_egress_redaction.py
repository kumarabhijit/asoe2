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
# Credit card: 13-19 digits, optionally separated by spaces / hyphens.
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
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
    out = _CC_RE.sub("<redacted-cc>", out)
    out = _IBAN_RE.sub("<redacted-iban>", out)
    return out
