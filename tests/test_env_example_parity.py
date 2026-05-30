"""Deployment-parity drift lock.

Every operator-facing env var read in non-test source must be documented
in `.env.example`, so local / Vercel-preview / Azure pre-prod can be
reconciled from one annotated file (docs/DEPLOYMENT_CONFIG_MATRIX.md) and
the example file cannot silently fall behind the code.

Scope: the `ASOE_`, `CORS_`, and `AZURE_` prefixes — the bulk of the
per-environment surface and the part most prone to drift. Provider SDK
vars (ANTHROPIC_*, OPENAI_*, …) are already documented and stable.

Dynamically-constructed names (e.g. `ASOE_LLM_PROVIDER_{task.upper()}`,
`ASOE_CANARY_PCT_{connector}`) are read via f-strings and so are NOT
extracted as literals here; their concrete instances are documented in
`.env.example` by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# Prefixes this lock enforces.
_PREFIXES = ("ASOE_", "CORS_", "AZURE_")

# String literal of an env var name, e.g. "ASOE_KILL_SWITCH".
_NAME_RE = re.compile(r"""["'](ASOE_|CORS_|AZURE_)[A-Z0-9_]+["']""")

# Directories that are runtime source (exclude tests, vendored, generated).
_SOURCE_DIRS = (
    "api", "constraints", "llm", "gateways", "compliance", "orchestration",
    "db", "contracts", "agents", "email_intelligence", "hardening", "redis",
    "skills", "recipes", "scripts",
)

# Genuinely non-operator / internal names that are intentionally NOT in
# `.env.example`. Keep this list SHORT and justified — every entry is a
# deliberate exception, not a parking lot.
_ALLOWLIST: frozenset[str] = frozenset()


def _read_source_names() -> dict[str, str]:
    """Map env-var-name → first file:line where it is read, for ASOE_/
    CORS_/AZURE_ literals across runtime source."""
    found: dict[str, str] = {}
    for d in _SOURCE_DIRS:
        root = _REPO_ROOT / d
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "/tests/" in path.as_posix():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # Only count actual reads, not comments/docstrings mentioning
                # the name. A read uses getenv/environ near the literal.
                if "getenv" not in line and "environ" not in line:
                    continue
                for m in _NAME_RE.finditer(line):
                    name = m.group(0).strip("\"'")
                    found.setdefault(name, f"{path.relative_to(_REPO_ROOT)}:{i}")
    return found


def test_env_example_documents_every_operator_var():
    documented = _ENV_EXAMPLE.read_text(encoding="utf-8")
    names = _read_source_names()
    assert names, "extractor found no env vars — the scan is broken"

    missing = {
        name: where
        for name, where in names.items()
        if name not in _ALLOWLIST and name not in documented
    }
    assert not missing, (
        "These env vars are read in source but undocumented in .env.example "
        "(deployment-parity drift). Document them (and add to "
        "docs/DEPLOYMENT_CONFIG_MATRIX.md) or, if truly internal, add to the "
        "allowlist with a justification:\n"
        + "\n".join(f"  {n}  ({w})" for n, w in sorted(missing.items()))
    )


def test_allowlist_entries_are_actually_read_somewhere():
    """Guard against a stale allowlist: every exception must still
    correspond to a real read, else it should be removed."""
    names = _read_source_names()
    stale = [n for n in _ALLOWLIST if n not in names]
    assert not stale, f"Allowlist entries no longer read in source: {stale}"
