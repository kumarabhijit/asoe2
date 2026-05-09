"""ADR-038 Phase H.1 — Knowledge bundle structure invariants.

These tests lock the L0 Knowledge Layer's bundle layout established in
ADR-038 §5.1–§5.4. Each existing skill MUST live as a bundle directory
under ``knowledge/skills/<name>/`` with a parseable ``metadata.yaml``,
the SKILL.md prose body, and the empty (Phase H.1) ``examples/`` /
``assets/`` / ``specs/`` subdirectories.

The tests also verify the loader's bundle-first resolution and the
LLM-backend skill-catalog walk consume the bundle layout end-to-end.
Drift on any of these is a regression of the H.1 contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from constraints.llm_backend import _load_skill_catalog
from skills.loader import SkillLoader

BUNDLE_ROOT = Path("knowledge/skills")

# The 9 skill bundles ADR-038 Phase H.1 migrated from flat SKILL.md
# files. Email-order-entry is intentionally absent — that bundle ships
# with the in-flight ADR-034 branch (see docs/plans/case-centric-rollout.md
# §3 Option C). The §3.4 coherence-fix commit added `email-order-entry`
# to the set so the H.1 invariant "every skill is a bundle" holds across
# the combined branch.
EXPECTED_BUNDLES: list[str] = [
    "back-order-resolution",
    "delivery-delay",
    "duplicate-po",
    "edi-mismatch",
    "email-order-entry",
    "moq-round-up",
    "over-max-trim",
    "pallet-alignment",
    "price-hold-release",
    "pricing-reconciliation",
]


# ---------------------------------------------------------------------------
# Bundle structure invariants
# ---------------------------------------------------------------------------


class TestBundleDirectoryLayout:
    """ADR-038 §5.1 — every skill is a bundle directory."""

    def test_bundle_root_exists(self):
        assert BUNDLE_ROOT.is_dir(), (
            f"knowledge/skills/ must exist after Phase H.1 migration; "
            f"missing at {BUNDLE_ROOT.resolve()}"
        )

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_bundle_dir_exists(self, bundle_name: str):
        assert (BUNDLE_ROOT / bundle_name).is_dir(), (
            f"Bundle {bundle_name!r} missing after migration"
        )

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_bundle_has_skill_md(self, bundle_name: str):
        skill_md = BUNDLE_ROOT / bundle_name / "SKILL.md"
        assert skill_md.is_file(), f"{skill_md} not found"

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_bundle_has_metadata_yaml(self, bundle_name: str):
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        assert manifest.is_file(), f"{manifest} not found"

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_bundle_has_examples_assets_specs_dirs(self, bundle_name: str):
        bundle = BUNDLE_ROOT / bundle_name
        for sub in ("examples", "assets", "specs"):
            assert (bundle / sub).is_dir(), (
                f"{bundle / sub} missing — Phase H.1 ships empty dirs "
                f"per ADR-038 §5.5 (examples earned by failures)"
            )


# ---------------------------------------------------------------------------
# metadata.yaml schema invariants (ADR-038 §5.2)
# ---------------------------------------------------------------------------


class TestMetadataYamlSchema:
    """ADR-038 §5.2 — manifest fields and constraints."""

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_metadata_parses_as_yaml(self, bundle_name: str):
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        with manifest.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        assert isinstance(data, dict), f"{manifest} did not parse to a mapping"

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_metadata_has_required_top_level_keys(self, bundle_name: str):
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        with manifest.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        for required in (
            "schema_version", "skill_name", "bundle_version",
            "recipes", "intents", "event_types",
            "anchor_examples", "on_demand_examples", "assets",
            "runtime_includes", "token_budget",
        ):
            assert required in data, (
                f"{manifest} missing required key {required!r}"
            )

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_bundle_version_is_semver(self, bundle_name: str):
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        with manifest.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        version = str(data["bundle_version"])
        parts = version.split(".")
        assert len(parts) == 3, f"bundle_version {version!r} is not SemVer"
        for part in parts:
            assert part.isdigit(), f"bundle_version part {part!r} is not numeric"

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_anchor_examples_count_at_most_two(self, bundle_name: str):
        """ADR-038 §5.4 rule 1 — anchor set is small (≤ 2)."""
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        with manifest.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        anchors = data.get("anchor_examples") or []
        assert len(anchors) <= 2, (
            f"{bundle_name}: anchor_examples count {len(anchors)} > 2 "
            f"violates ADR-038 §5.4 rule 1"
        )

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_token_budget_cached_prefix_at_most_3000(self, bundle_name: str):
        """ADR-038 §5.4 rule 4 — per-skill cached-prefix budget."""
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        with manifest.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        budget = data.get("token_budget") or {}
        cached_max = budget.get("cached_prefix_max_tokens", 0)
        assert isinstance(cached_max, int), (
            f"{bundle_name}: cached_prefix_max_tokens must be an int"
        )
        assert cached_max <= 3000, (
            f"{bundle_name}: cached_prefix_max_tokens {cached_max} > 3000 "
            f"violates ADR-038 §5.4 rule 4"
        )

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_runtime_includes_does_not_list_specs(self, bundle_name: str):
        """ADR-038 §5.4 rule 3 — specs/ are runtime:false."""
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        with manifest.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        runtime_includes = data.get("runtime_includes") or []
        for entry in runtime_includes:
            assert "specs" not in str(entry), (
                f"{bundle_name}: specs/ path {entry!r} cannot be in "
                f"runtime_includes (ADR-038 §5.4 rule 3 — humans-only)"
            )

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_metadata_skill_name_matches_directory(self, bundle_name: str):
        manifest = BUNDLE_ROOT / bundle_name / "metadata.yaml"
        with manifest.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        # The manifest's skill_name carries the canonical skill name from
        # the SKILL.md frontmatter (e.g. "duplicate-po"). Bundle directory
        # names must match.
        assert data["skill_name"] == bundle_name, (
            f"{manifest}: skill_name {data['skill_name']!r} does not match "
            f"bundle directory {bundle_name!r}"
        )


# ---------------------------------------------------------------------------
# Loader integration — bundles are picked up end-to-end
# ---------------------------------------------------------------------------


class TestLoaderBundleResolution:
    """ADR-038 §5.3 — the loader resolves bundle paths preferentially."""

    @pytest.mark.parametrize("bundle_name", EXPECTED_BUNDLES)
    def test_load_by_legacy_filename_resolves_to_bundle(self, bundle_name: str):
        """Backward-compat: callers still pass legacy filenames; the
        loader resolves to the bundle path under the hood."""
        legacy_filename = f"{bundle_name}_SKILL.md"
        loader = SkillLoader("skills")
        doc = loader.load_by_name(legacy_filename)
        # The doc must carry the same content as the bundle's SKILL.md
        # (proves the bundle path was used, not a stale legacy file).
        bundle_text = (BUNDLE_ROOT / bundle_name / "SKILL.md").read_text(
            encoding="utf-8",
        )
        assert doc.text == bundle_text

    def test_discover_returns_one_doc_per_bundle(self):
        loader = SkillLoader("skills")
        docs = loader.discover()
        assert len(docs) == len(EXPECTED_BUNDLES), (
            f"discover() returned {len(docs)} docs; expected "
            f"{len(EXPECTED_BUNDLES)} (one per bundle)"
        )

    def test_missing_skill_raises_with_bundle_path_in_error(self):
        loader = SkillLoader("skills")
        with pytest.raises(FileNotFoundError) as excinfo:
            loader.load_by_name("nonexistent_SKILL.md")
        # The error message must mention the canonical bundle path so
        # the operator immediately knows where to look.
        assert "knowledge/skills/nonexistent" in str(excinfo.value)


# ---------------------------------------------------------------------------
# LLM backend integration — skill catalog walks bundles
# ---------------------------------------------------------------------------


class TestLLMBackendCatalogWalk:
    """The cacheable skill catalog the LLM backend builds for prompt
    caching MUST source from the bundle layout. ADR-038 §5.3."""

    def test_catalog_includes_every_bundle_skill_md(self):
        catalog = _load_skill_catalog()
        assert catalog, "skill catalog is empty post-migration"
        for bundle_name in EXPECTED_BUNDLES:
            assert f"{bundle_name}/SKILL.md" in catalog, (
                f"catalog missing the {bundle_name}/SKILL.md marker"
            )

    def test_catalog_is_alphabetical_for_cache_stability(self):
        """Bundle order must be alphabetical so the byte sequence is
        stable across worker pods (ADR-038 §5.3 cache discipline)."""
        catalog = _load_skill_catalog()
        positions = {
            bundle_name: catalog.find(f"{bundle_name}/SKILL.md")
            for bundle_name in EXPECTED_BUNDLES
        }
        sorted_by_position = sorted(positions.items(), key=lambda kv: kv[1])
        sorted_alphabetically = sorted(positions.items(), key=lambda kv: kv[0])
        assert sorted_by_position == sorted_alphabetically, (
            "skill catalog is not alphabetical — cache hits will fragment"
        )

    def test_catalog_bytes_are_stable_across_calls(self):
        """Sanity check on the cache itself."""
        first = _load_skill_catalog()
        second = _load_skill_catalog()
        assert first == second
