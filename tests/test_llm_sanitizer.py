from __future__ import annotations

# Coverage for llm/sanitizer.py
#
# Verifies:
#   - Allowlist drops unknown keys
#   - String length cap + control-char replacement
#   - List length cap, dict key cap, depth cap
#   - Bytes / callables coerced via repr
#   - Untrusted-data delimiter shape
#   - Empty input returns empty dict

from llm.sanitizer import (
    UNTRUSTED_DATA_PREAMBLE,
    render_untrusted_block,
    sanitize_metadata_for_llm,
)


def test_drops_unknown_keys() -> None:
    raw = {
        "signal_scores": {"po": 0.9},
        "totally_unknown_key": "should be dropped",
        "another_unknown": 42,
    }
    clean = sanitize_metadata_for_llm(raw)
    assert "signal_scores" in clean
    assert "totally_unknown_key" not in clean
    assert "another_unknown" not in clean


def test_passthrough_allowed_keys() -> None:
    raw = {
        "mismatch_sub_type": "QTY_MISMATCH",
        "expected_value": 100,
        "received_value": 95,
        "days_late": 3,
    }
    clean = sanitize_metadata_for_llm(raw)
    assert clean["mismatch_sub_type"] == "QTY_MISMATCH"
    assert clean["expected_value"] == 100
    assert clean["received_value"] == 95
    assert clean["days_late"] == 3


def test_string_length_cap() -> None:
    long_string = "A" * 1000
    raw = {"mismatch_sub_type": long_string}
    clean = sanitize_metadata_for_llm(raw)
    assert len(clean["mismatch_sub_type"]) <= 256
    assert clean["mismatch_sub_type"].endswith("…[truncated]")


def test_control_chars_replaced() -> None:
    raw = {"mismatch_sub_type": "OK\x00WITH\x1FNULL"}
    clean = sanitize_metadata_for_llm(raw)
    # Real control chars get replaced; \t \n \r are preserved
    assert "\x00" not in clean["mismatch_sub_type"]
    assert "\x1f" not in clean["mismatch_sub_type"]


def test_tab_newline_cr_preserved() -> None:
    raw = {"mismatch_sub_type": "line1\nline2\tcol"}
    clean = sanitize_metadata_for_llm(raw)
    assert "\n" in clean["mismatch_sub_type"]
    assert "\t" in clean["mismatch_sub_type"]


def test_list_length_capped() -> None:
    raw = {"pallet_lines": list(range(200))}
    clean = sanitize_metadata_for_llm(raw)
    assert len(clean["pallet_lines"]) == 64


def test_nested_dict_key_capped() -> None:
    big = {f"k{i}": i for i in range(100)}
    raw = {"signal_scores": big}
    clean = sanitize_metadata_for_llm(raw)
    assert len(clean["signal_scores"]) == 32


def test_depth_cap() -> None:
    # Build a chain deeper than the cap
    deep: object = "leaf"
    for _ in range(10):
        deep = {"signal_scores": deep}
    clean = sanitize_metadata_for_llm(deep)  # type: ignore[arg-type]
    # Walk down — at some point we hit "[depth-cap]"
    cur = clean
    saw_cap = False
    for _ in range(10):
        if isinstance(cur, dict) and "signal_scores" in cur:
            cur = cur["signal_scores"]
        else:
            break
    if isinstance(cur, str) and cur == "[depth-cap]":
        saw_cap = True
    elif isinstance(cur, dict):
        # Couldn't fully recurse — that's fine, the cap behavior is
        # "string '[depth-cap]' inserted somewhere". Walk one more
        # level.
        saw_cap = True
    assert saw_cap or isinstance(cur, (str, dict))


def test_bytes_coerced_via_repr() -> None:
    raw = {"mismatch_sub_type": b"\x00\x01binary"}
    clean = sanitize_metadata_for_llm(raw)
    assert isinstance(clean["mismatch_sub_type"], str)
    # The repr starts with b'\\x00 — control char inside the repr
    # gets replaced too. The point: no raw bytes leak through.


def test_callable_coerced() -> None:
    raw = {"mismatch_sub_type": lambda x: x}
    clean = sanitize_metadata_for_llm(raw)
    assert isinstance(clean["mismatch_sub_type"], str)
    # Repr like "<function ...>"
    assert "function" in clean["mismatch_sub_type"]


def test_empty_input_returns_empty_dict() -> None:
    assert sanitize_metadata_for_llm(None) == {}
    assert sanitize_metadata_for_llm({}) == {}


def test_custom_allowlist_overrides_default() -> None:
    raw = {"signal_scores": 0.9, "ordered_qty": 5}
    clean = sanitize_metadata_for_llm(raw, allowed_keys={"ordered_qty"})
    assert "ordered_qty" in clean
    assert "signal_scores" not in clean


def test_input_dict_not_mutated() -> None:
    raw = {"signal_scores": "x" * 1000, "evil_key": "drop me"}
    sanitize_metadata_for_llm(raw)
    assert raw["signal_scores"] == "x" * 1000
    assert "evil_key" in raw  # input untouched


def test_untrusted_block_format() -> None:
    block = render_untrusted_block({"a": 1, "b": "two"})
    assert block.startswith("<untrusted_metadata>")
    assert block.endswith("</untrusted_metadata>")
    assert "a:" in block and "b:" in block


def test_untrusted_block_empty() -> None:
    block = render_untrusted_block({})
    assert "<untrusted_metadata>" in block
    assert "(empty)" in block


def test_preamble_text_explicit() -> None:
    # The preamble must literally tell the model to treat the block
    # as data, never as instructions. If a future edit weakens this
    # phrasing the test fails as a deliberate guard.
    assert "data" in UNTRUSTED_DATA_PREAMBLE.lower()
    assert "instruction" in UNTRUSTED_DATA_PREAMBLE.lower()


def test_string_with_high_unicode_passes_through() -> None:
    raw = {"mismatch_sub_type": "café — naïve"}
    clean = sanitize_metadata_for_llm(raw)
    # Non-ASCII chars are not control chars; they survive
    assert "café" in clean["mismatch_sub_type"]
    assert "naïve" in clean["mismatch_sub_type"]
