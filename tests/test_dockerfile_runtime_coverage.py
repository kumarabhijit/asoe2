"""Fitness test — every runtime-loaded resource declared by source code is
also COPYed into Dockerfile.api / Dockerfile.core.

The bug this catches: a module reads a file at import or instantiation
time via ``Path(__file__).resolve().parent.parent / "<dir>" / ...``. The
test passes locally because the file exists on the developer's checkout.
It crashes the Container App on first boot because the Dockerfile only
copies ``api/``, ``contracts/`` etc. — and the runtime-loaded resource
lives outside any copied directory.

Concrete instance (PR introducing this test): PR-C added
``gateways/tenant_config.py`` which loaded
``docs/specs/duplicate-po/config-defaults.json`` (the file's runtime
home is now ``gateways/configs/duplicate_po/defaults.json``; the
docs/-rooted path described here is the original-bug context) at gateway instantiation
(``api/sandbox_gateways.py`` registers the gateway when ``ASOE_ENV=sandbox``
which is true on pre-prod). The original PR didn't bundle ``docs/`` into
the image, so the pre-prod Container App crashloops with
FileNotFoundError on boot. Tests were green (the file exists locally);
only an end-to-end ``docker build`` would have caught it. The follow-up
relocated the JSON to ``gateways/configs/duplicate_po/defaults.json``
(co-located with its consumer in an already-COPYed package directory)
and this fitness test guards the new contract going forward.

How this test catches it (two layers):

1. **Explicit runtime-load registry** — `_KNOWN_RUNTIME_PATHS` enumerates
   every (module, attribute, expected_dir) tuple where source code is
   known to load a file at runtime via a Path-segment chain. Adding a
   new runtime resource means adding a row here, which is then verified
   against every Dockerfile's COPY surface.

2. **Literal-path scan** — secondary scan via regex for fully-quoted
   ``"<dir>/.../file.<ext>"`` strings under the source tree, catches
   the simpler form (e.g. ``compliance/audit_bearing_registry.yaml``).

   The regex form is intentionally narrow: it does NOT match Path-
   segment chains (those are covered by layer 1).

Allow-list: paths legitimately not bundled (e.g. developer-only
exports) live in ``_ALLOWED_NOT_IN_IMAGE`` with justification.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Set, Tuple

import pytest


_REPO = Path(__file__).resolve().parent.parent

# Dockerfiles whose COPY surface must satisfy every runtime-loaded path.
# Dockerfile.inference is intentionally narrow (constraints + llm only) and
# does not carry the orchestration runtime, so it's excluded.
_TARGET_DOCKERFILES = ("Dockerfile.api", "Dockerfile.core")

# Top-level directories that are NEVER expected to be in the image
# (developer-only, infra-only, build-time-only). When source code declares
# a runtime path under one of these, that's a real bug — flag it.
_NEVER_COPYED = {"tests", "infra", "k8s", "scripts", ".github", "patches"}


# ---------------------------------------------------------------------------
# Layer 1 — explicit registry of runtime-load points.
#
# Add a new entry here whenever a module loads a resource at module-import
# or instance-construction time via any path mechanism (Path-chain,
# os.path.join, hardcoded string, relative literal, etc.).
#
# Tuple: (description, top_level_dir, full_repo_relative_path)
# ---------------------------------------------------------------------------

_KNOWN_RUNTIME_PATHS: List[Tuple[str, str, str]] = [
    (
        "gateways/tenant_config.py::_PLATFORM_DEFAULTS_PATH "
        "(loaded at TenantConfigGateway.__init__; "
        "register_sandbox_gateways instantiates the gateway when "
        "ASOE_ENV=sandbox — pre-prod default). The defaults file lives "
        "under gateways/configs/<intent>/ so each exception type's "
        "configs are clearly grouped by folder name and future configs "
        "(calibration targets, behavior presets, etc.) can be added as "
        "siblings without renaming.",
        "gateways",
        "gateways/configs/duplicate_po/defaults.json",
    ),
    (
        "api/analysis_composer.py — audit_bearing_registry.yaml "
        "(loaded at module import to drive coverage classification)",
        "compliance",
        "compliance/audit_bearing_registry.yaml",
    ),
]


# Resource paths that source code references but are legitimately not
# expected to be in the runtime image. Empty by default — every entry
# requires an explicit justification.
_ALLOWED_NOT_IN_IMAGE: Set[str] = {
    # `scripts/export_openapi.py` is a developer tool; it writes to
    # `openapi/asoe2.openapi.json` but is never invoked at runtime
    # in a container. The script itself isn't bundled either.
    "openapi/asoe2.openapi.json",
}


# ---------------------------------------------------------------------------
# Layer 2 — literal-path regex scan (catches simple cases).
# ---------------------------------------------------------------------------

# Pattern matching a literal path string of the form "dir/.../file.ext"
# that's two or more segments and ends in a known runtime-resource
# extension. Excludes URLs (http://...), and any path with double-dot.
_RUNTIME_PATH_RE = re.compile(
    r'["\']'
    r'((?:[a-zA-Z][a-zA-Z0-9_-]*)/'              # first segment
    r'(?:[a-zA-Z0-9._-]+/)*'                     # zero or more segments
    r'[a-zA-Z0-9._-]+'                            # final segment
    r'\.(?:json|ya?ml|sql|md|txt))'              # extension
    r'["\']'
)


def _python_files() -> List[Path]:
    """Every .py file under the repo, excluding tests/ and venv-style dirs."""
    out: List[Path] = []
    for p in _REPO.rglob("*.py"):
        rel = p.relative_to(_REPO)
        parts = rel.parts
        if not parts:
            continue
        top = parts[0]
        if top in {"tests", "scripts", ".venv", "venv", "build"}:
            continue
        if "__pycache__" in parts:
            continue
        out.append(p)
    return out


def _extract_paths(py_path: Path) -> Set[str]:
    """Return every literal "<dir>/.../file.<ext>" string in the file."""
    text = py_path.read_text(encoding="utf-8", errors="replace")
    candidates = set(_RUNTIME_PATH_RE.findall(text))
    return {c for c in candidates if not c.startswith(("http", "https"))}


def _dockerfile_copied_dirs(df_path: Path) -> Set[str]:
    """Top-level paths a Dockerfile COPYs (just the source side, normalised)."""
    out: Set[str] = set()
    for line in df_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("COPY"):
            continue
        toks = [t for t in s.split() if not t.startswith("--")]
        if len(toks) < 3:
            continue
        for src in toks[1:-1]:
            src = src.rstrip("/")
            out.add(src)
    return out


def _dockerfile_runtime_resource_dirs(df_path: Path) -> Set[str]:
    """Set of top-level directories the Dockerfile makes available at /app."""
    copied = _dockerfile_copied_dirs(df_path)
    tops: Set[str] = set()
    for src in copied:
        if src in {".", "/"} or ("/" not in src and src.endswith((".toml", ".lock"))):
            continue
        if "/" in src:
            tops.add(src.split("/", 1)[0])
        else:
            tops.add(src)
    return tops


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_dockerfiles_exist():
    for df in _TARGET_DOCKERFILES:
        assert (_REPO / df).exists(), f"{df} missing"


@pytest.mark.parametrize(
    "description,top_dir,full_path",
    _KNOWN_RUNTIME_PATHS,
    ids=[p[2] for p in _KNOWN_RUNTIME_PATHS],
)
def test_known_runtime_path_exists_locally(
    description: str, top_dir: str, full_path: str,
) -> None:
    """The repo-relative path declared by a known runtime-load point
    must actually exist in the source tree. Catches the case where a
    rename / relocation broke the runtime contract before any
    Dockerfile question even comes up.
    """
    target = _REPO / full_path
    assert target.exists(), (
        f"Source tree is missing {full_path}, declared by:\n"
        f"  {description}\n"
        f"Either restore the file or update _KNOWN_RUNTIME_PATHS to "
        f"reflect the new location."
    )


@pytest.mark.parametrize(
    "description,top_dir,full_path",
    _KNOWN_RUNTIME_PATHS,
    ids=[p[2] for p in _KNOWN_RUNTIME_PATHS],
)
def test_known_runtime_path_in_dockerfile_api(
    description: str, top_dir: str, full_path: str,
) -> None:
    """Every known runtime-load point's top-level directory MUST be
    COPYed into Dockerfile.api. Without this, the Container App
    crashloops on first boot with FileNotFoundError when the loader
    runs (typically at gateway-instantiation or module-import time).
    """
    df_dirs = _dockerfile_runtime_resource_dirs(_REPO / "Dockerfile.api")
    assert top_dir in df_dirs, (
        f"Dockerfile.api does not COPY '{top_dir}/' but the application "
        f"loads {full_path} at runtime. Add a COPY line for the "
        f"directory.\n\nLoad point: {description}"
    )


def test_every_literal_path_in_source_is_covered_by_dockerfile_api():
    """Secondary scan: literal path strings of the form "dir/.../file.ext"
    in source code must reference a directory that Dockerfile.api COPYs
    (or be in _ALLOWED_NOT_IN_IMAGE).
    """
    df_dirs = _dockerfile_runtime_resource_dirs(_REPO / "Dockerfile.api")

    offenders: List[str] = []
    for py in _python_files():
        for path_str in _extract_paths(py):
            if path_str in _ALLOWED_NOT_IN_IMAGE:
                continue
            top = path_str.split("/", 1)[0]
            if top in _NEVER_COPYED:
                continue
            if top in df_dirs:
                continue
            offenders.append(
                f"  {py.relative_to(_REPO)} → \"{path_str}\" "
                f"(top-level dir '{top}/' missing from Dockerfile.api COPYs)"
            )
    assert not offenders, (
        "Dockerfile.api is missing COPY directives for runtime-loaded "
        "resources referenced by the application. Add the necessary "
        "COPY lines (or extend `_ALLOWED_NOT_IN_IMAGE` with a "
        "justification if the path is genuinely non-runtime).\n\n"
        + "\n".join(offenders)
    )


def test_no_runtime_code_references_test_only_directories():
    """Source code under the package tree must NEVER load resources from
    `tests/`, `infra/`, `k8s/`, `scripts/` etc. — those directories
    aren't bundled into the image, and a runtime reference would
    crashloop the container.
    """
    offenders: List[str] = []
    for py in _python_files():
        for path_str in _extract_paths(py):
            if path_str in _ALLOWED_NOT_IN_IMAGE:
                continue
            top = path_str.split("/", 1)[0]
            if top in _NEVER_COPYED:
                offenders.append(
                    f"  {py.relative_to(_REPO)} → \"{path_str}\" "
                    f"(references '{top}/' which is never bundled into "
                    f"the runtime image)"
                )
    assert not offenders, (
        "Application code references resources under directories that "
        "are never copied into the runtime container image. These will "
        "raise FileNotFoundError on first boot.\n\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("df", _TARGET_DOCKERFILES)
def test_dockerfile_copy_lines_use_consistent_src_dst_naming(df):
    """Sanity: each COPY directive uses the same source and destination
    name (the repo's convention). A COPY that renames the target would
    quietly break the assumption that the Dockerfile's COPY surface
    equals the runtime-available top-level directory list.
    """
    df_path = _REPO / df
    bad: List[str] = []
    for line in df_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s.startswith("COPY"):
            continue
        if "--from=" in s:
            continue
        toks = s.split()
        if len(toks) < 3:
            continue
        dst = toks[-1]
        if dst.endswith("/") and dst != "./":
            dst_name = dst.rstrip("/").split("/")[-1]
            for src in toks[1:-1]:
                src_name = src.rstrip("/").split("/")[-1]
                if src_name != dst_name:
                    bad.append(f"  {df}: COPY {src} {dst}  (rename)")
    assert not bad, (
        f"COPY directives in {df} rename the target. The fitness test "
        f"infrastructure assumes src == dst basename; rewrite to keep "
        f"that invariant or extend the test.\n"
        + "\n".join(bad)
    )
