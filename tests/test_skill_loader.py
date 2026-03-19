from __future__ import annotations

# Phase 1.1 — Skill Loader tests
#
# Verifies that:
#   - Skills are loaded verbatim (no summarisation)
#   - Skill selection is event-type-driven
#   - Parsed metadata (name, description, recipes) is correct
#   - The loader never loads more than what is needed for the event

import pytest

from contracts.models import SkillDocument
from skills.loader import SkillLoader


SKILLS_ROOT = "skills"


# ---------------------------------------------------------------------------
# load_by_name
# ---------------------------------------------------------------------------

class TestLoadByName:
    def test_returns_skill_document(self):
        doc = SkillLoader(SKILLS_ROOT).load_by_name("pricing-reconciliation_SKILL.md")
        assert isinstance(doc, SkillDocument)

    def test_name_parsed_correctly(self):
        doc = SkillLoader(SKILLS_ROOT).load_by_name("pricing-reconciliation_SKILL.md")
        assert doc.name == "pricing-reconciliation"

    def test_description_is_non_empty(self):
        doc = SkillLoader(SKILLS_ROOT).load_by_name("pricing-reconciliation_SKILL.md")
        assert len(doc.description) > 0

    def test_text_is_verbatim(self):
        """Skill text must be injected verbatim — not summarised or truncated."""
        doc = SkillLoader(SKILLS_ROOT).load_by_name("pricing-reconciliation_SKILL.md")
        # Spot-check key sections that must survive verbatim
        assert "Compliance Shadow" in doc.text
        assert "PriceAdjustmentRecipe.py" in doc.text
        assert "CreditHoldReleaseRecipe.py" in doc.text
        assert "FAIL_TO_HUMAN" in doc.text

    def test_recipes_list_parsed(self):
        doc = SkillLoader(SKILLS_ROOT).load_by_name("pricing-reconciliation_SKILL.md")
        assert "PriceAdjustmentRecipe.py" in doc.recipes
        assert "CreditHoldReleaseRecipe.py" in doc.recipes

    def test_text_length_matches_file(self):
        """Text must not be truncated."""
        import pathlib
        raw = pathlib.Path(SKILLS_ROOT, "pricing-reconciliation_SKILL.md").read_text(encoding="utf-8")
        doc = SkillLoader(SKILLS_ROOT).load_by_name("pricing-reconciliation_SKILL.md")
        assert doc.text == raw

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            SkillLoader(SKILLS_ROOT).load_by_name("nonexistent_SKILL.md")


# ---------------------------------------------------------------------------
# select_for_event — event-type routing
# ---------------------------------------------------------------------------

class TestSelectForEvent:
    def test_edi_850_routes_to_pricing_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("EDI_850_PRICE_MISMATCH")
        assert doc.name == "pricing-reconciliation"

    def test_price_keyword_routes_to_pricing_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("PRICE_VARIANCE")
        assert doc.name == "pricing-reconciliation"

    def test_edi_850_case_insensitive(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("edi_850_price_mismatch")
        assert doc.name == "pricing-reconciliation"

    def test_returned_skill_contains_execution_protocol(self):
        """Skill must include compliance shadow instruction — required by CLAUDE.md."""
        doc = SkillLoader(SKILLS_ROOT).select_for_event("EDI_850_PRICE_MISMATCH")
        assert "Compliance Shadow" in doc.text

    def test_returned_skill_contains_constrained_generation_policy(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("EDI_850_PRICE_MISMATCH")
        assert "Guidance" in doc.text or "Outlines" in doc.text


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

class TestDiscover:
    def test_discovers_at_least_one_skill(self):
        docs = SkillLoader(SKILLS_ROOT).discover()
        assert len(docs) >= 1

    def test_all_discovered_docs_are_skill_documents(self):
        docs = SkillLoader(SKILLS_ROOT).discover()
        for doc in docs:
            assert isinstance(doc, SkillDocument)

    def test_discovered_docs_have_non_empty_names(self):
        docs = SkillLoader(SKILLS_ROOT).discover()
        for doc in docs:
            assert doc.name and doc.name != "unknown-skill"
