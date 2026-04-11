from __future__ import annotations

import re
from pathlib import Path
from typing import List

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

    def select_for_event(self, event_type: str) -> SkillDocument:
        upper = event_type.upper()
        if "DUPLICATE" in upper:
            return self.load_by_name("duplicate-po_SKILL.md")
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
