"""Pure-function unit tests for the EDI 850 builder (ADR-042 Phase 5).

The builder (`gateways/edi850.build_edi_850`) is the deterministic, fully
unit-testable port of the prototype's `buildEDI850`. These tests lock the X12
segment structure, the deterministic control numbers, the CTT/SE totals, and
the round-trip into the `Edi850Document` contract. No I/O, no clock, no LLM.
"""

from __future__ import annotations

from api.schemas import Edi850Document
from gateways.edi850 import build_edi_850


def _order(**overrides):
    base = dict(
        order_id="ORD-EDI-001",
        po_number="0093847612",
        po_date="2025-03-17",
        buyer_name="Walmart Stores Inc",
        buyer_id="300001",
        seller_name="Acme Beverages Co",
        seller_id="VENDOR-7788",
        currency="USD",
        requested_date="2025-03-24",
        line_items=[
            {"line_num": "001", "material": "BEV-COLA-12PK",
             "description": "Cola 12-pack case", "quantity": 480,
             "uom": "CS", "unit_price": 8.64},
            {"line_num": "002", "material": "BEV-LEMON-6PK",
             "description": "Lemon 6-pack case", "quantity": 120,
             "uom": "CS", "unit_price": 5.50},
        ],
    )
    base.update(overrides)
    return base


def test_builds_full_x12_envelope_and_validates_contract():
    doc = build_edi_850(**_order())
    # Round-trips into the typed section contract.
    model = Edi850Document(**doc)
    assert model.standard == "ANSI X12 5010"
    assert model.transaction_set == "850"
    assert model.envelope.sender_id == "300001"      # buyer transmits the 850
    assert model.envelope.receiver_id == "VENDOR-7788"
    assert model.header.po_number == "0093847612"
    assert model.header.purpose_code == "00"
    assert model.header.po_type == "SA"


def test_segment_order_and_grouping():
    doc = build_edi_850(**_order())
    seg_ids = [s["seg_id"] for s in doc["segments"]]
    # Envelope opens with ISA/GS/ST and closes with SE/GE/IEA.
    assert seg_ids[:3] == ["ISA", "GS", "ST"]
    assert seg_ids[-3:] == ["SE", "GE", "IEA"]
    # Header + parties + lines + totals all present.
    for required in ("BEG", "CUR", "REF", "DTM", "N1", "PO1", "PID", "CTT"):
        assert required in seg_ids, required
    groups = {s["seg_id"]: s["group"] for s in doc["segments"]}
    assert groups["ISA"] == "envelope"
    assert groups["DTM"] == "dates"
    assert groups["N1"] == "party"
    assert groups["PO1"] == "line"
    assert groups["CTT"] == "trailer"


def test_each_segment_is_decoded_and_raw_terminated():
    doc = build_edi_850(**_order())
    for seg in doc["segments"]:
        assert seg["raw"].startswith(seg["seg_id"] + "*")
        assert seg["raw"].endswith("~")
        assert seg["meaning"]  # every segment carries a human decode
    # raw_x12 is the concatenation of the per-segment raw lines.
    assert doc["raw_x12"] == "\n".join(s["raw"] for s in doc["segments"])
    assert doc["raw_x12"].startswith("ISA*")
    assert doc["raw_x12"].rstrip().endswith("IEA*1*" + doc["envelope"]["interchange_control_number"] + "~")


def test_totals_count_quantity_and_amount():
    doc = build_edi_850(**_order())
    assert doc["totals"]["total_line_items"] == 2
    assert doc["totals"]["total_quantity"] == 600.0          # 480 + 120
    # 480*8.64 + 120*5.50 = 4147.2 + 660.0
    assert doc["totals"]["total_amount"] == 4807.2
    line = doc["line_items"][0]
    assert line["extended_amount"] == round(480 * 8.64, 2)
    assert line["product_qualifier"] == "VP"
    assert line["product_id"] == "BEV-COLA-12PK"


def test_ctt_and_se_segment_values():
    doc = build_edi_850(**_order())
    segs = {s["seg_id"]: s["elements"] for s in doc["segments"]}
    assert segs["CTT"][0] == "2"          # line-item count
    # SE01 = number of segments ST..SE inclusive; SE02 echoes the ST control.
    se = next(s for s in doc["segments"] if s["seg_id"] == "SE")
    st = next(s for s in doc["segments"] if s["seg_id"] == "ST")
    seg_ids = [s["seg_id"] for s in doc["segments"]]
    st_pos = seg_ids.index("ST")
    se_pos = seg_ids.index("SE")
    assert se["elements"][0] == str(se_pos - st_pos + 1)
    assert se["elements"][1] == st["elements"][1]


def test_deterministic_for_fixed_inputs():
    a = build_edi_850(**_order())
    b = build_edi_850(**_order())
    assert a == b
    # control numbers are derived from order_id, not a clock/sequence.
    c = build_edi_850(**_order(order_id="ORD-EDI-999"))
    assert (c["envelope"]["interchange_control_number"]
            != a["envelope"]["interchange_control_number"])


def test_ship_to_loop_is_optional():
    without = build_edi_850(**_order())
    assert [p["entity_code"] for p in without["parties"]] == ["BY", "SE"]
    withst = build_edi_850(**_order(ship_to_name="Walmart DC #6094"))
    codes = [p["entity_code"] for p in withst["parties"]]
    assert codes == ["BY", "SE", "ST"]
    st = withst["parties"][2]
    assert st["role"] == "Ship To"
    assert st["name"] == "Walmart DC #6094"


def test_line_without_price_leaves_amount_unset():
    doc = build_edi_850(**_order(line_items=[
        {"line_num": "001", "material": "BEV-COLA-12PK",
         "quantity": 10, "uom": "CS"},
    ]))
    line = doc["line_items"][0]
    assert line["unit_price"] is None
    assert line["extended_amount"] is None
    assert doc["totals"]["total_amount"] is None
    assert doc["totals"]["total_quantity"] == 10.0


def test_empty_order_still_builds_valid_envelope():
    doc = build_edi_850(**_order(line_items=[]))
    Edi850Document(**doc)  # validates
    assert doc["totals"]["total_line_items"] == 0
    assert doc["totals"]["total_quantity"] == 0.0
    assert doc["totals"]["total_amount"] is None
    seg_ids = [s["seg_id"] for s in doc["segments"]]
    assert "PO1" not in seg_ids
    assert seg_ids[-3:] == ["SE", "GE", "IEA"]
