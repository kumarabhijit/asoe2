from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from contracts.models import SkillDocument

META_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
RECIPES_RE = re.compile(r"^\s*recipes:\s*\[(.*?)\]", re.MULTILINE)

# ADR-038 §5.1 — knowledge bundle root. The bundle directory layout is
# knowledge/skills/<bundle-name>/SKILL.md (+ metadata.yaml + examples/
# + assets/ + specs/). The loader prefers this path; legacy
# skills/<name>_SKILL.md is kept as a fallback for one release per
# ADR-038 Phase H.1 backward-compat policy.
BUNDLE_ROOT = Path("knowledge/skills")


def _bundle_name_from_legacy_filename(name: str) -> str:
    """Map a legacy skill filename to its bundle directory name.

    ``"duplicate-po_SKILL.md"`` → ``"duplicate-po"``.
    ``"duplicate-po"``           → ``"duplicate-po"`` (already a bundle name).

    Returns the original string when the suffix isn't present so callers
    that pass a bundle name directly still resolve correctly.
    """
    if name.endswith("_SKILL.md"):
        return name[: -len("_SKILL.md")]
    if name.endswith(".md"):
        return name[: -len(".md")]
    return name


class SkillLoader:
    """Loader for skill documents.

    Phase H.1 of ADR-038 introduces the bundle layout. ``load_by_name`` and
    ``discover`` prefer ``BUNDLE_ROOT/<bundle>/SKILL.md`` and fall back to
    the legacy ``<root>/<filename>`` path. This keeps existing call sites
    (``skills/loader.py::select_for_event`` consumers, tests passing
    ``"skills"`` as the constructor arg) working without modification —
    they pick up the migrated bundles transparently.
    """

    # Class-level cache keyed by resolved path — skills are static .md files
    # that don't change during process lifetime.  Shared across instances.
    _cache: dict[str, SkillDocument] = {}

    def __init__(self, root: str | Path = "skills") -> None:
        # Legacy root retained for backward-compat; new code may pass
        # ``BUNDLE_ROOT`` directly. Either way, ``_resolve`` checks the
        # bundle path first.
        self.root = Path(root)
        self.bundle_root = BUNDLE_ROOT

    def _resolve(self, name: str) -> Path:
        """Return the canonical path for the named skill, preferring the
        bundle path over the legacy flat-file path.

        Raises ``FileNotFoundError`` only when neither exists.
        """
        bundle_name = _bundle_name_from_legacy_filename(name)
        bundle_path = self.bundle_root / bundle_name / "SKILL.md"
        if bundle_path.exists():
            return bundle_path
        legacy_path = self.root / name
        if legacy_path.exists():
            return legacy_path
        # Surface the bundle path in the error so the operator knows the
        # canonical location even when nothing is present.
        raise FileNotFoundError(
            f"Skill not found: tried bundle path {bundle_path!s} and legacy "
            f"path {legacy_path!s}"
        )

    def load_by_name(self, name: str) -> SkillDocument:
        path = self._resolve(name)
        key = str(path)
        if key not in self._cache:
            text = path.read_text(encoding="utf-8")
            self._cache[key] = self._parse(text)
        return self._cache[key]

    def discover(self) -> List[SkillDocument]:
        """Discover all skills available to the loader.

        Prefers bundles under ``BUNDLE_ROOT``; falls back to legacy
        ``<root>/*.md`` only when no bundles are present at all (e.g.
        an isolated test fixture or a pre-migration repo). Bundle order
        is alphabetical by directory name; legacy order is alphabetical
        by filename.
        """
        bundle_paths = sorted(
            p / "SKILL.md"
            for p in self.bundle_root.iterdir()
            if p.is_dir() and (p / "SKILL.md").exists()
        ) if self.bundle_root.exists() else []
        if bundle_paths:
            return [self._parse(p.read_text(encoding="utf-8")) for p in bundle_paths]
        # Legacy fallback — only used when no bundles exist.
        legacy_paths = sorted(self.root.glob("*.md"))
        return [self._parse(p.read_text(encoding="utf-8")) for p in legacy_paths]

    def select_for_event(
        self,
        event_type: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SkillDocument:
        """Select the skill file matching an event's semantic category.

        For ``EDI_850_LINE_MISMATCH`` the ``metadata.mismatch_sub_type``
        refines the routing: ``PRICE_MISMATCH`` loads the
        pricing-reconciliation skill (handed off to CONTRACTUAL_CORRECTION),
        every other sub_type loads ``edi-mismatch_SKILL.md``.

        ``metadata`` is optional so existing call sites that only have the
        event_type string keep working; when absent, the router falls back
        to the coarser event-type match.
        """
        upper = event_type.upper()
        if "EMAIL_ORDER_ENTRY" in upper or "EMAIL_ORDER" in upper:
            return self.load_by_name("email-order-entry_SKILL.md")
        if "DUPLICATE" in upper:
            return self.load_by_name("duplicate-po_SKILL.md")
        # Check PRICE_HOLD before the broader PRICE/EDI_850 fork — the
        # pricing-reconciliation skill would otherwise swallow held-order
        # events whose intent is PRICE_HOLD_RELEASE, not CONTRACTUAL_CORRECTION.
        if "PRICE_HOLD" in upper:
            return self.load_by_name("price-hold-release_SKILL.md")
        # Line-mismatch events fork on metadata.mismatch_sub_type so the
        # PRICE_MISMATCH deferral to the pricing path loads a coherent skill.
        if "LINE_MISMATCH" in upper or "EDI_MISMATCH" in upper:
            sub_type = (metadata or {}).get("mismatch_sub_type")
            if sub_type == "PRICE_MISMATCH":
                return self.load_by_name("pricing-reconciliation_SKILL.md")
            return self.load_by_name("edi-mismatch_SKILL.md")
        # OM-adjacent intents — each event type maps to exactly one skill.
        if "BACK_ORDER" in upper or "OOS" in upper:
            return self.load_by_name("back-order-resolution_SKILL.md")
        if "OVER_MAX" in upper:
            return self.load_by_name("over-max-trim_SKILL.md")
        if "MIN_ORDER_QTY" in upper or "MOQ" in upper:
            return self.load_by_name("moq-round-up_SKILL.md")
        if "PALLET_CONFIG" in upper or "PALLET" in upper:
            return self.load_by_name("pallet-alignment_SKILL.md")
        if "DELIVERY_DELAY" in upper:
            return self.load_by_name("delivery-delay_SKILL.md")
        if "PRICE" in upper or "EDI_850" in upper:
            return self.load_by_name("pricing-reconciliation_SKILL.md")
        return self.discover()[0]

    def _parse(self, text: str) -> SkillDocument:
        meta = META_RE.search(text)
        meta_text = meta.group(1) if meta else ""
        name = NAME_RE.search(meta_text)
        desc = DESC_RE.search(meta_text)
        recipes_match = RECIPES_RE.search(meta_text)
        recipes = []
        if recipes_match:
            raw = recipes_match.group(1)
            recipes = [r.strip() for r in raw.split(",") if r.strip()]
        return SkillDocument(
            name=name.group(1).strip() if name else "unknown-skill",
            description=desc.group(1).strip() if desc else "",
            text=text,
            recipes=recipes,
        )
