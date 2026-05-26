"""JWKS fetch + cache for Entra ID (Azure AD) token validation — PARITY-3b.

The validator at ``api.deps._entra_decode`` calls
``get_jwk_for_kid(issuer_url, kid)`` to resolve the public key for a
given token. The function maintains a per-issuer cache with a 1h TTL
and refreshes on an unknown ``kid`` (handles Entra's silent key
rotation).

Failure modes the Security review flagged as CRITICAL:

* JWKS-fetch timeout MUST fail closed — we raise an ASOEError(401)
  rather than letting the validator silently accept the token.
* An unknown ``kid`` MUST trigger a JWKS refresh; the validator must
  not silently fall back to "first key in the cached JWKS".

Tested by ``tests/test_azure_ad_jwks.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.request import urlopen

from api.errors import ASOEError

logger = logging.getLogger("asoe.api.azure_ad_jwks")

# Per-issuer cache: { issuer_url: (expires_at_unix, jwks_dict) }
_CACHE: Dict[str, tuple[float, Dict[str, Any]]] = {}

_DEFAULT_TTL_SECONDS = 3600
_DEFAULT_TIMEOUT_SECONDS = 5.0


def _ttl_seconds() -> float:
    raw = os.getenv("ASOE_JWKS_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
        return value if value > 0 else _DEFAULT_TTL_SECONDS
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _fetch_timeout_seconds() -> float:
    raw = os.getenv("ASOE_JWKS_FETCH_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
        return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS


def reset_cache() -> None:
    """Drop all cached JWKS. Test-helper; safe to call from prod too."""
    _CACHE.clear()


def _jwks_uri_for_issuer(issuer_url: str) -> str:
    """Map an Entra issuer URL to its discovery document → jwks_uri.

    Entra's OIDC well-known doc lives at ``<issuer>/.well-known/openid-configuration``;
    the discovery doc carries ``jwks_uri``. For Entra v2 the convention
    is ``<issuer>/discovery/v2.0/keys`` and we shortcut to it rather
    than doing the OIDC discovery round trip on every miss.
    """
    base = issuer_url.rstrip("/")
    return f"{base}/discovery/v2.0/keys"


def _fetch_jwks_raw(issuer_url: str) -> Dict[str, Any]:
    """Fetch the JWKS document for the issuer.

    Isolated as a module-level function so tests can monkeypatch it
    without touching the cache + retry logic.
    """
    uri = _jwks_uri_for_issuer(issuer_url)
    try:
        with urlopen(uri, timeout=_fetch_timeout_seconds()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError) as exc:
        raise TimeoutError(f"JWKS fetch failed for {uri}: {exc}") from exc


def _load_jwks(issuer_url: str, force_refresh: bool = False) -> Dict[str, Any]:
    now = time.time()
    cached = _CACHE.get(issuer_url)
    if cached and not force_refresh and now < cached[0]:
        return cached[1]
    try:
        jwks = _fetch_jwks_raw(issuer_url)
    except TimeoutError as exc:
        logger.error("JWKS fetch timed out for %s — failing closed", issuer_url)
        raise ASOEError(
            code="JWKS_UNAVAILABLE",
            message="Identity provider unavailable.",
            status_code=401,
        ) from exc
    _CACHE[issuer_url] = (now + _ttl_seconds(), jwks)
    return jwks


def get_jwk_for_kid(issuer_url: str, kid: str) -> Optional[Dict[str, Any]]:
    """Return the JWK matching ``kid`` for ``issuer_url``, or None.

    Refreshes the JWKS cache when ``kid`` is unknown — Entra rotates
    signing keys without notice, and silently treating an unknown kid
    as "use whatever's cached" is the classic JWKS-rotation footgun.
    """
    jwks = _load_jwks(issuer_url, force_refresh=False)
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    # Unknown kid → force a refresh and retry once.
    jwks = _load_jwks(issuer_url, force_refresh=True)
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None
