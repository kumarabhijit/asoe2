from __future__ import annotations

# EDI 850 deterministic builder (ADR-042 Phase 5 — the EDI 850 Audit tab).
#
# A pure, fully unit-testable port of the prototype's client-side
# `buildEDI850(email)`. It reconstructs the ANSI X12 5010 *Purchase Order*
# (transaction set 850) the system would transmit for a reviewed order, so an
# operator can audit the wire-format document before it reaches the seller's
# ERP. EDI 850 flows BUYER → SELLER: in the Customer-Inbox flow the customer is
# the buyer and the ASOE tenant is the seller, so the seller identity is the
# tenant's own (passed in by the producer), never fabricated third-party data.
#
# Guardrail #1/#2 (recipes/builders execute; orchestration routes): all segment
# construction lives here as a pure function — no I/O, no LLM, no clock, no
# randomness. The same inputs always yield byte-identical output (control
# numbers are derived from `order_id` via CRC32, never a sequence/clock), which
# is what makes the X12 document auditable and the unit tests deterministic.
#
# Output is the `api.schemas.Edi850Document` shape (a plain dict; the composer
# projects it into the typed contract — Guardrail #6). It carries the three
# prototype sub-views: Decoded (envelope/header/parties/line_items/totals),
# Raw X12 (`raw_x12`), and Segment Map (`segments`, each with raw + decoded
# meaning + colour group).

import zlib
from typing import Any, Dict, List, Optional, Tuple

# X12 delimiters (5010 conventional).
_ELEMENT_SEP = "*"
_SEGMENT_TERM = "~"

_X12_VERSION = "005010"
_GS_VERSION = "005010"

# Segment → viewer colour bucket (prototype EDI_SEG_META parity).
_SEGMENT_GROUP: Dict[str, str] = {
    "ISA": "envelope", "GS": "envelope", "ST": "envelope",
    "BEG": "header", "CUR": "header", "REF": "header",
    "DTM": "dates",
    "N1": "party", "N3": "party", "N4": "party",
    "PO1": "line", "PID": "line",
    "CTT": "trailer", "SE": "trailer", "GE": "trailer", "IEA": "trailer",
}

# Segment → human-readable decode (Segment Map view).
_SEGMENT_MEANING: Dict[str, str] = {
    "ISA": "Interchange Control Header",
    "GS": "Functional Group Header",
    "ST": "Transaction Set Header",
    "BEG": "Beginning Segment for Purchase Order",
    "CUR": "Currency",
    "REF": "Reference Identification",
    "DTM": "Date/Time Reference",
    "N1": "Party Identification",
    "N3": "Party Location (Address)",
    "N4": "Geographic Location",
    "PO1": "Baseline Item Data",
    "PID": "Product/Item Description",
    "CTT": "Transaction Totals",
    "SE": "Transaction Set Trailer",
    "GE": "Functional Group Trailer",
    "IEA": "Interchange Control Trailer",
}

# N101 entity-identifier code → decoded role label.
_PARTY_ROLE: Dict[str, str] = {
    "BY": "Buying Party (Purchaser)",
    "SE": "Selling Party",
    "ST": "Ship To",
}


def build_edi_850(
    *,
    order_id: str,
    po_number: str,
    po_date: str,
    buyer_name: str,
    seller_name: str,
    buyer_id: Optional[str] = None,
    seller_id: Optional[str] = None,
    ship_to_name: Optional[str] = None,
    currency: str = "USD",
    requested_date: Optional[str] = None,
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the X12 5010 EDI 850 document for a reviewed order.

    Args:
      order_id: host trace / order identifier; seeds the deterministic
        interchange/group/transaction control numbers.
      po_number: the customer purchase-order number (BEG03 / REF*PO).
      po_date: PO date, ISO ``YYYY-MM-DD`` (BEG05 / ISA09 / GS04).
      buyer_name / buyer_id: the purchasing customer (N1*BY).
      seller_name / seller_id: the ASOE tenant org (N1*SE) — tenant identity,
        supplied by the producer; never fabricated here.
      ship_to_name: optional consignee (N1*ST); the loop is omitted when absent.
      currency: ISO currency code (CUR02), default ``USD``.
      requested_date: optional requested delivery date, ISO (DTM*002).
      line_items: list of ``{line_num, material, description, quantity, uom,
        unit_price}`` dicts → one PO1 (+ optional PID) each.

    Returns the ``api.schemas.Edi850Document`` shape as a plain dict. Pure: no
    side effects, deterministic for fixed inputs.
    """
    lines = line_items or []

    isa_date = _yymmdd(po_date)
    isa_time = "0000"
    gs_date = _ymd(po_date)

    sender = (buyer_id or buyer_name or "BUYER").upper()[:15]
    receiver = (seller_id or seller_name or "SELLER").upper()[:15]
    isa_ctrl = _control(order_id + "|isa", 9)
    gs_ctrl = _control(order_id + "|gs", 9).lstrip("0") or "1"
    st_ctrl = _control(order_id + "|st", 4)

    # --- assemble segments in transmission order ---------------------------
    segs: List[Tuple[str, List[str]]] = []

    segs.append((
        "ISA",
        ["00", _pad("", 10), "00", _pad("", 10), "ZZ", _pad(sender, 15),
         "ZZ", _pad(receiver, 15), isa_date, isa_time, "U", _X12_VERSION,
         isa_ctrl, "0", "P", ">"],
    ))
    segs.append(("GS", ["PO", sender.strip(), receiver.strip(), gs_date,
                        isa_time, gs_ctrl, "X", _GS_VERSION]))
    segs.append(("ST", ["850", st_ctrl]))

    # PO header
    segs.append(("BEG", ["00", "SA", po_number, "", gs_date]))
    segs.append(("CUR", ["BY", currency]))
    segs.append(("REF", ["PO", po_number]))
    segs.append(("DTM", ["004", gs_date]))  # 004 = PO date
    if requested_date:
        segs.append(("DTM", ["002", _ymd(requested_date)]))  # 002 = requested

    # Party loops (N1[/N3/N4])
    parties: List[Dict[str, Any]] = []
    parties.append(_party("BY", buyer_name, buyer_id))
    parties.append(_party("SE", seller_name, seller_id))
    if ship_to_name:
        parties.append(_party("ST", ship_to_name, None))
    for p in parties:
        n1 = ["N1", p["entity_code"], p["name"]]
        if p["id_value"]:
            n1 += [p["id_qualifier"] or "92", p["id_value"]]
        segs.append((n1[0], n1[1:]))

    # Line items (PO1 + PID)
    out_lines: List[Dict[str, Any]] = []
    total_qty = 0.0
    total_amount: Optional[float] = None
    for raw in lines:
        line_num = str(raw.get("line_num", "") or str(len(out_lines) + 1))
        qty = _as_float(raw.get("quantity")) or 0.0
        uom = str(raw.get("uom") or "EA")
        price = _as_float(raw.get("unit_price"))
        material = raw.get("material")
        description = raw.get("description")
        extended = round(qty * price, 2) if price is not None else None

        po1 = ["PO1", line_num, _num(qty), uom]
        if price is not None:
            po1 += [_num(price), "PE"]  # PE = price per each
        else:
            po1 += ["", ""]
        if material:
            po1 += ["VP", str(material)]
        segs.append((po1[0], po1[1:]))
        if description:
            segs.append(("PID", ["F", "", "", "", str(description)]))

        total_qty += qty
        if extended is not None:
            total_amount = round((total_amount or 0.0) + extended, 2)
        out_lines.append({
            "line_num": line_num,
            "quantity": qty,
            "uom": uom,
            "unit_price": price,
            "product_qualifier": "VP" if material else None,
            "product_id": str(material) if material else None,
            "description": str(description) if description else None,
            "extended_amount": extended,
        })

    # Totals + trailers. SE count = ST … SE inclusive.
    segs.append(("CTT", [str(len(out_lines)), _num(total_qty)]))
    se_index = _index_of(segs, "ST")
    se_count = len(segs) - se_index + 1  # +1 for the SE we are about to add
    segs.append(("SE", [str(se_count), st_ctrl]))
    segs.append(("GE", ["1", gs_ctrl]))
    segs.append(("IEA", ["1", isa_ctrl]))

    # --- project the structured + raw views --------------------------------
    rendered = [_render(seg_id, els) for seg_id, els in segs]
    raw_x12 = "\n".join(r["raw"] for r in rendered)

    envelope = {
        "sender_id": sender.strip(),
        "receiver_id": receiver.strip(),
        "interchange_control_number": isa_ctrl,
        "group_control_number": gs_ctrl,
        "transaction_set_control_number": st_ctrl,
        "usage_indicator": "P",
        "x12_version": _X12_VERSION,
    }
    header = {
        "purpose_code": "00",
        "po_type": "SA",
        "po_number": po_number,
        "po_date": po_date,
        "currency": currency,
        "requested_delivery_date": requested_date,
    }

    return {
        "standard": "ANSI X12 5010",
        "transaction_set": "850",
        "envelope": envelope,
        "header": header,
        "parties": parties,
        "line_items": out_lines,
        "totals": {
            "total_line_items": len(out_lines),
            "total_quantity": round(total_qty, 3),
            "total_amount": total_amount,
        },
        "segments": rendered,
        "raw_x12": raw_x12,
    }


# ---------------------------------------------------------------------------
# helpers (pure)
# ---------------------------------------------------------------------------

def _render(seg_id: str, elements: List[str]) -> Dict[str, Any]:
    raw = _ELEMENT_SEP.join([seg_id, *elements]) + _SEGMENT_TERM
    return {
        "seg_id": seg_id,
        "elements": list(elements),
        "raw": raw,
        "meaning": _SEGMENT_MEANING.get(seg_id, seg_id),
        "group": _SEGMENT_GROUP.get(seg_id, "header"),
    }


def _party(entity_code: str, name: str,
           id_value: Optional[str]) -> Dict[str, Any]:
    return {
        "entity_code": entity_code,
        "role": _PARTY_ROLE.get(entity_code, entity_code),
        "name": name,
        "id_qualifier": "92" if id_value else None,
        "id_value": id_value,
        "address": None,
        "city_state_zip": None,
    }


def _control(seed: str, width: int) -> str:
    return str(zlib.crc32(seed.encode("utf-8")) % (10 ** width)).rjust(width, "0")


def _ymd(iso: str) -> str:
    return (iso or "").replace("-", "")


def _yymmdd(iso: str) -> str:
    return _ymd(iso)[2:]


def _pad(value: str, width: int) -> str:
    return (value or "")[:width].ljust(width)


def _num(value: float) -> str:
    """X12 numeric: integers render without a decimal point, else trimmed."""
    if value == int(value):
        return str(int(value))
    return ("%f" % value).rstrip("0").rstrip(".")


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _index_of(segs: List[Tuple[str, List[str]]], seg_id: str) -> int:
    for i, (sid, _) in enumerate(segs):
        if sid == seg_id:
            return i
    return 0
