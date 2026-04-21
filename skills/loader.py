from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from contracts.models import SkillDocument

META_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
RECIPES_RE = re.compile(r"^\s*recipes:\s*\[(.*?)\]", re.MULTILINE)


class SkillLoader:
    # Class-level cache keyed by resolved path — skills are static .md files
    # that don't change during process lifetime.  Shared across instances.
    _cache: dict[str, SkillDocument] = {}

    def __init__(self, root: str | Path = "skills") -> None:
        self.root = Path(root)

    def load_by_name(self, name: str) -> SkillDocument:
        key = str(self.root / name)
        if key not in self._cache:
            text = (self.root / name).read_text(encoding="utf-8")
            self._cache[key] = self._parse(text)
        return self._cache[key]

    def discover(self) -> List[SkillDocument]:
        docs = []
        for path in sorted(self.root.glob("*.md")):
            docs.append(self._parse(path.read_text(encoding="utf-8")))
        return docs

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
