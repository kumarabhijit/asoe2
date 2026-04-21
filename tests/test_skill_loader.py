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


# ---------------------------------------------------------------------------
# select_for_event — PRICE_HOLD_RELEASE routing (ordering matters)
# ---------------------------------------------------------------------------

class TestPriceHoldReleaseRouting:
    def test_edi_850_price_hold_routes_to_price_hold_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("EDI_850_PRICE_HOLD")
        assert doc.name == "price-hold-release"

    def test_price_hold_beats_price_fallback(self):
        """PRICE_HOLD must be matched before the broader PRICE fallback;
        otherwise the pricing-reconciliation skill would swallow the event."""
        doc = SkillLoader(SKILLS_ROOT).select_for_event("EDI_850_PRICE_HOLD")
        assert doc.name != "pricing-reconciliation"

    def test_price_hold_case_insensitive(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("edi_850_price_hold")
        assert doc.name == "price-hold-release"


# ---------------------------------------------------------------------------
# select_for_event — EDI_MISMATCH routing with sub_type fork
# ---------------------------------------------------------------------------

class TestEdiMismatchRouting:
    def test_line_mismatch_without_sub_type_routes_to_edi_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("EDI_850_LINE_MISMATCH")
        assert doc.name == "edi-mismatch"

    def test_sku_mismatch_routes_to_edi_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event(
            "EDI_850_LINE_MISMATCH",
            metadata={"mismatch_sub_type": "SKU_MISMATCH"},
        )
        assert doc.name == "edi-mismatch"

    def test_qty_mismatch_routes_to_edi_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event(
            "EDI_850_LINE_MISMATCH",
            metadata={"mismatch_sub_type": "QTY_MISMATCH"},
        )
        assert doc.name == "edi-mismatch"

    def test_ship_to_mismatch_routes_to_edi_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event(
            "EDI_850_LINE_MISMATCH",
            metadata={"mismatch_sub_type": "SHIP_TO_MISMATCH"},
        )
        assert doc.name == "edi-mismatch"

    def test_price_mismatch_forks_to_pricing_skill(self):
        """PRICE_MISMATCH sub_type must route to pricing-reconciliation so
        the skill text matches the CONTRACTUAL_CORRECTION intent that the
        classifier assigns (single source of truth for pricing)."""
        doc = SkillLoader(SKILLS_ROOT).select_for_event(
            "EDI_850_LINE_MISMATCH",
            metadata={"mismatch_sub_type": "PRICE_MISMATCH"},
        )
        assert doc.name == "pricing-reconciliation"

    def test_metadata_absent_defaults_to_edi_skill(self):
        """When metadata is None (old callers), fallback must still pick the
        EDI mismatch skill — never silently route to pricing."""
        doc = SkillLoader(SKILLS_ROOT).select_for_event(
            "EDI_850_LINE_MISMATCH",
            metadata=None,
        )
        assert doc.name == "edi-mismatch"


# ---------------------------------------------------------------------------
# select_for_event — OM-adjacent intent routing (5 new intents)
# ---------------------------------------------------------------------------

class TestOMAdjacentIntentRouting:
    def test_back_order_event_routes_to_back_order_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("BACK_ORDER_OOS")
        assert doc.name == "back-order-resolution"

    def test_over_max_event_routes_to_trim_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("OVER_MAX_QTY")
        assert doc.name == "over-max-trim"

    def test_moq_event_routes_to_round_up_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("MIN_ORDER_QTY")
        assert doc.name == "moq-round-up"

    def test_pallet_config_event_routes_to_pallet_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("PALLET_CONFIG_VIOLATION")
        assert doc.name == "pallet-alignment"

    def test_delivery_delay_event_routes_to_delay_skill(self):
        doc = SkillLoader(SKILLS_ROOT).select_for_event("DELIVERY_DELAY")
        assert doc.name == "delivery-delay"
